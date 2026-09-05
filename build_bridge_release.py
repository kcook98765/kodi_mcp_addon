#!/usr/bin/env python3
"""Build and verify a deterministic service.kodi_mcp release bundle.

Release payload bytes are read from an exact Git commit, never from the mutable
working tree. The ZIP contains non-self-referential build provenance; the
external bridge-bootstrap.json binds it to the final artifact hash and size.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import stat
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree


ADDON_ID = "service.kodi_mcp"
BOOTSTRAP_SCHEMA_VERSION = 1
BUILDER_FORMAT_VERSION = 1
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
FIXED_FILE_MODE = stat.S_IFREG | 0o644
MAX_MEMBERS = 256
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PackagingError(RuntimeError):
    """The requested release bundle is unsafe, inconsistent, or unverifiable."""


@dataclass(frozen=True)
class SourceFile:
    path: str
    git_mode: str
    data: bytes


def _git(repo: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PackagingError(f"Git command failed: {' '.join(args)}") from exc


def _require_clean_repository(repo: Path) -> None:
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PackagingError("release builds require a clean Git working tree")


def resolve_commit(repo: Path, revision: str) -> str:
    resolved = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").decode().strip()
    if not _GIT_SHA_RE.fullmatch(resolved):
        raise PackagingError("resolved source commit is not a full lowercase Git SHA")
    return resolved


def _validate_relative_path(path: str) -> str:
    if not path or "\\" in path:
        raise PackagingError(f"unsafe source path: {path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PackagingError(f"unsafe source path: {path!r}")
    normalized = pure.as_posix()
    if normalized != path:
        raise PackagingError(f"non-canonical source path: {path!r}")
    return normalized


def read_commit_files(repo: Path, commit: str) -> list[SourceFile]:
    raw = _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    files: list[SourceFile] = []
    folded_paths: set[str] = set()
    total_bytes = 0
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii").split(" ", 2)
            path = _validate_relative_path(raw_path.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise PackagingError("malformed or non-UTF-8 Git tree entry") from exc
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise PackagingError(f"unsupported Git tree entry {mode} {object_type} {path}")
        folded = path.casefold()
        if folded in folded_paths:
            raise PackagingError(f"duplicate or case-fold-colliding source path: {path}")
        folded_paths.add(folded)
        data = _git(repo, "cat-file", "blob", object_id)
        if len(data) > MAX_FILE_BYTES:
            raise PackagingError(f"source file exceeds size limit: {path}")
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_BYTES:
            raise PackagingError("source payload exceeds total size limit")
        files.append(SourceFile(path=path, git_mode=mode, data=data))
    files.sort(key=lambda item: item.path)
    if not files or len(files) + 1 > MAX_MEMBERS:
        raise PackagingError("source payload has an invalid member count")
    if any(item.path == "build_manifest.json" for item in files):
        raise PackagingError("source tree must not contain generated build_manifest.json")
    return files


def source_fingerprint(files: list[SourceFile]) -> str:
    """Hash canonical source paths and bytes independently of ZIP metadata."""
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda entry: entry.path):
        digest.update(item.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.data)
        digest.update(b"\0")
    return digest.hexdigest()


def _addon_identity(files: list[SourceFile]) -> tuple[str, str]:
    by_path = {item.path: item.data for item in files}
    try:
        root = ElementTree.fromstring(by_path["addon.xml"])
    except KeyError as exc:
        raise PackagingError("source commit is missing addon.xml") from exc
    except ElementTree.ParseError as exc:
        raise PackagingError("source commit has invalid addon.xml") from exc
    addon_id = str(root.get("id") or "")
    version = str(root.get("version") or "")
    if addon_id != ADDON_ID:
        raise PackagingError(f"addon id is {addon_id!r}, expected {ADDON_ID!r}")
    if not version:
        raise PackagingError("addon version is missing")
    return addon_id, version


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIMESTAMP)
    info.create_system = 3
    info.create_version = 20
    info.extract_version = 20
    info.flag_bits = 0
    info.external_attr = FIXED_FILE_MODE << 16
    info.internal_attr = 0
    info.compress_type = zipfile.ZIP_STORED
    info.comment = b""
    info.extra = b""
    return info


def _build_zip(files: list[SourceFile], build_manifest: bytes) -> bytes:
    members = [(item.path, item.data) for item in files]
    members.append(("build_manifest.json", build_manifest))
    members.sort(key=lambda item: item[0])
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.comment = b""
        for relative, data in members:
            archive.writestr(_zip_info(f"{ADDON_ID}/{relative}"), data)
    return output.getvalue()


def build_release(repo: Path | str, output_dir: Path | str, *, revision: str = "HEAD") -> dict[str, Any]:
    repo_path = Path(repo).resolve()
    output_path = Path(output_dir).resolve()
    _require_clean_repository(repo_path)
    commit = resolve_commit(repo_path, revision)
    files = read_commit_files(repo_path, commit)
    addon_id, version = _addon_identity(files)
    fingerprint = source_fingerprint(files)
    embedded_manifest = {
        "builder_format_version": BUILDER_FORMAT_VERSION,
        "source_fingerprint_sha256": fingerprint,
        "source_git_sha": commit,
    }
    zip_bytes = _build_zip(files, _json_bytes(embedded_manifest))
    artifact_name = f"{addon_id}-{version}.zip"
    artifact_sha256 = hashlib.sha256(zip_bytes).hexdigest()
    external_manifest = {
        "addon_id": addon_id,
        "artifact": artifact_name,
        "artifact_sha256": artifact_sha256,
        "artifact_size_bytes": len(zip_bytes),
        "builder_format_version": BUILDER_FORMAT_VERSION,
        "schema_version": BOOTSTRAP_SCHEMA_VERSION,
        "source_fingerprint_sha256": fingerprint,
        "source_git_sha": commit,
        "version": version,
    }
    output_path.mkdir(parents=True, exist_ok=True)
    artifact_path = output_path / artifact_name
    manifest_path = output_path / "bridge-bootstrap.json"
    artifact_path.write_bytes(zip_bytes)
    manifest_path.write_bytes(_json_bytes(external_manifest))
    result = verify_release(repo_path, manifest_path)
    result.update({"artifact": str(artifact_path), "manifest": str(manifest_path)})
    return result


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackagingError("bootstrap manifest is unreadable") from exc
    if not isinstance(value, dict):
        raise PackagingError("bootstrap manifest must be a JSON object")
    required = {
        "schema_version", "builder_format_version", "addon_id", "version", "artifact",
        "artifact_sha256", "artifact_size_bytes", "source_git_sha", "source_fingerprint_sha256",
    }
    if set(value) != required:
        raise PackagingError("bootstrap manifest fields are not canonical")
    return value


def verify_release(repo: Path | str, manifest: Path | str) -> dict[str, Any]:
    repo_path = Path(repo).resolve()
    manifest_path = Path(manifest).resolve()
    payload = _load_manifest(manifest_path)
    if payload["schema_version"] != BOOTSTRAP_SCHEMA_VERSION:
        raise PackagingError("unsupported bootstrap manifest schema")
    if payload["builder_format_version"] != BUILDER_FORMAT_VERSION:
        raise PackagingError("unsupported builder format")
    if payload["addon_id"] != ADDON_ID:
        raise PackagingError("bootstrap manifest addon id mismatch")
    if not _GIT_SHA_RE.fullmatch(str(payload["source_git_sha"])):
        raise PackagingError("bootstrap manifest source commit is invalid")
    if not _SHA256_RE.fullmatch(str(payload["source_fingerprint_sha256"])):
        raise PackagingError("bootstrap manifest source fingerprint is invalid")
    if not _SHA256_RE.fullmatch(str(payload["artifact_sha256"])):
        raise PackagingError("bootstrap manifest artifact SHA-256 is invalid")
    artifact_name = str(payload["artifact"])
    if Path(artifact_name).name != artifact_name:
        raise PackagingError("bootstrap artifact must be beside its manifest")
    artifact_path = manifest_path.parent / artifact_name
    try:
        artifact_bytes = artifact_path.read_bytes()
    except OSError as exc:
        raise PackagingError("bootstrap artifact is missing") from exc
    if len(artifact_bytes) != payload["artifact_size_bytes"]:
        raise PackagingError("bootstrap artifact size mismatch")
    if hashlib.sha256(artifact_bytes).hexdigest() != payload["artifact_sha256"]:
        raise PackagingError("bootstrap artifact SHA-256 mismatch")

    commit = resolve_commit(repo_path, str(payload["source_git_sha"]))
    if commit != payload["source_git_sha"]:
        raise PackagingError("bootstrap source commit is not canonical")
    files = read_commit_files(repo_path, commit)
    addon_id, version = _addon_identity(files)
    fingerprint = source_fingerprint(files)
    if addon_id != payload["addon_id"] or version != payload["version"]:
        raise PackagingError("bootstrap addon identity does not match source commit")
    if fingerprint != payload["source_fingerprint_sha256"]:
        raise PackagingError("bootstrap source fingerprint does not match source commit")

    expected_source = {item.path: item.data for item in files}
    expected_names = {f"{ADDON_ID}/{path}" for path in expected_source}
    expected_names.add(f"{ADDON_ID}/build_manifest.json")
    try:
        with zipfile.ZipFile(io.BytesIO(artifact_bytes), "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or len({name.casefold() for name in names}) != len(names):
                raise PackagingError("ZIP contains duplicate normalized paths")
            if set(names) != expected_names:
                raise PackagingError("ZIP payload does not match the exact source commit")
            embedded: Any = None
            for info in infos:
                relative = info.filename.removeprefix(f"{ADDON_ID}/")
                _validate_relative_path(relative)
                mode = (info.external_attr >> 16) & 0xFFFF
                if (
                    info.is_dir() or info.date_time != FIXED_ZIP_TIMESTAMP or info.create_system != 3
                    or info.create_version != 20 or info.extract_version != 20 or info.flag_bits != 0
                    or mode != FIXED_FILE_MODE or info.compress_type != zipfile.ZIP_STORED
                    or info.extra or info.comment
                ):
                    raise PackagingError("ZIP member metadata is not canonical")
                data = archive.read(info)
                if relative == "build_manifest.json":
                    embedded = json.loads(data.decode("utf-8"))
                elif data != expected_source[relative]:
                    raise PackagingError(f"ZIP payload differs from source commit: {relative}")
    except PackagingError:
        raise
    except (KeyError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise PackagingError("bootstrap ZIP is invalid") from exc

    expected_embedded = {
        "builder_format_version": BUILDER_FORMAT_VERSION,
        "source_fingerprint_sha256": fingerprint,
        "source_git_sha": commit,
    }
    if embedded != expected_embedded:
        raise PackagingError("embedded build provenance does not match the source commit")
    return {
        "ok": True,
        "addon_id": addon_id,
        "version": version,
        "source_git_sha": commit,
        "source_fingerprint_sha256": fingerprint,
        "artifact_sha256": payload["artifact_sha256"],
        "artifact_size_bytes": payload["artifact_size_bytes"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="build an exact deterministic release bundle")
    build.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent)
    build.add_argument("--commit", default="HEAD")
    build.add_argument("--output-dir", type=Path, default=Path("dist"))
    verify = subparsers.add_parser("verify", help="verify a release bundle against Git objects")
    verify.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent)
    verify.add_argument("--manifest", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = (
            build_release(args.repo, args.output_dir, revision=args.commit)
            if args.command == "build"
            else verify_release(args.repo, args.manifest)
        )
    except PackagingError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
