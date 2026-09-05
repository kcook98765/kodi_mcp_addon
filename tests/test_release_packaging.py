from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

import build_bridge_release as release


MERGED_MAIN = "d9791d334c7ead485ac72550e79d6803909d5cec"
MERGED_FINGERPRINT = "6afbd37b1e4d392da67ad88073ea0ed4fe7a5a1468ce8a2edfae57c20a1b8fd3"


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path, *, addon_id: str = release.ADDON_ID, version: str = "0.2.40") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q")
    _run(repo, "config", "user.name", "Release Test")
    _run(repo, "config", "user.email", "release@example.invalid")
    (repo / "addon.xml").write_text(
        f'<addon id="{addon_id}" version="{version}" name="Test" provider-name="Test"/>\n',
        encoding="utf-8",
    )
    (repo / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    resources = repo / "resources"
    resources.mkdir()
    (resources / "settings.xml").write_text("<settings/>\n", encoding="utf-8")
    _run(repo, "add", ".")
    _run(repo, "commit", "-qm", "fixture")
    return repo


def _manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@contextmanager
def _umask(value: int):
    previous = os.umask(value)
    try:
        yield
    finally:
        os.umask(previous)


def test_merged_0_2_39_source_fingerprint_is_preserved():
    repo = Path(__file__).parents[1]
    files = release.read_commit_files(repo, release.resolve_commit(repo, MERGED_MAIN))
    assert release.source_fingerprint(files) == MERGED_FINGERPRINT


def test_same_commit_is_byte_identical_across_directories_mtimes_timezones_and_umasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = _repo(tmp_path)
    commit = _run(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()
    with _umask(0o077):
        first = release.build_release(repo, tmp_path / "one", revision=commit)
    for path in repo.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            os.utime(path, (1_700_000_000, 1_700_000_000))
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    if hasattr(time, "tzset"):
        time.tzset()
    with _umask(0o022):
        second = release.build_release(repo, tmp_path / "two", revision=commit)
    assert Path(first["artifact"]).read_bytes() == Path(second["artifact"]).read_bytes()
    assert Path(first["manifest"]).read_bytes() == Path(second["manifest"]).read_bytes()


def test_entry_input_order_does_not_change_zip():
    files = [
        release.SourceFile("service.py", "100644", b"pass\n"),
        release.SourceFile("addon.xml", "100755", b"<addon/>\n"),
    ]
    manifest = b"{}\n"
    assert release._build_zip(files, manifest) == release._build_zip(list(reversed(files)), manifest)


def test_dirty_worktree_fails_closed(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "service.py").write_text("DIRTY = True\n", encoding="utf-8")
    with pytest.raises(release.PackagingError, match="clean Git working tree"):
        release.build_release(repo, tmp_path / "out")


def test_manifest_binds_commit_fingerprint_zip_hash_and_size(tmp_path: Path):
    repo = _repo(tmp_path)
    result = release.build_release(repo, tmp_path / "out")
    manifest_path = Path(result["manifest"])
    payload = _manifest(manifest_path)
    artifact = Path(result["artifact"])
    assert payload["source_git_sha"] == _run(repo, "rev-parse", "HEAD")
    assert payload["source_fingerprint_sha256"] == result["source_fingerprint_sha256"]
    assert payload["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert payload["artifact_size_bytes"] == artifact.stat().st_size
    assert release.verify_release(repo, manifest_path)["ok"] is True


def test_zip_metadata_and_archive_root_are_canonical(tmp_path: Path):
    repo = _repo(tmp_path)
    result = release.build_release(repo, tmp_path / "out")
    with zipfile.ZipFile(result["artifact"]) as archive:
        names = [item.filename for item in archive.infolist()]
        assert names == sorted(names)
        assert all(name.startswith(release.ADDON_ID + "/") for name in names)
        for item in archive.infolist():
            assert item.date_time == release.FIXED_ZIP_TIMESTAMP
            assert item.create_system == 3
            assert item.create_version == 20
            assert item.extract_version == 20
            assert item.flag_bits == 0
            assert (item.external_attr >> 16) & 0xFFFF == release.FIXED_FILE_MODE
            assert item.compress_type == zipfile.ZIP_STORED


def test_payload_change_changes_fingerprint_and_zip(tmp_path: Path):
    repo = _repo(tmp_path)
    first = release.build_release(repo, tmp_path / "one")
    (repo / "service.py").write_text("VALUE = 2\n", encoding="utf-8")
    _run(repo, "add", "service.py")
    _run(repo, "commit", "-qm", "change payload")
    second = release.build_release(repo, tmp_path / "two")
    assert first["source_git_sha"] != second["source_git_sha"]
    assert first["source_fingerprint_sha256"] != second["source_fingerprint_sha256"]
    assert first["artifact_sha256"] != second["artifact_sha256"]


def test_new_commit_with_same_tree_changes_provenance_and_zip_only(tmp_path: Path):
    repo = _repo(tmp_path)
    first = release.build_release(repo, tmp_path / "one")
    _run(repo, "commit", "--allow-empty", "-qm", "new provenance")
    second = release.build_release(repo, tmp_path / "two")
    assert first["source_git_sha"] != second["source_git_sha"]
    assert first["source_fingerprint_sha256"] == second["source_fingerprint_sha256"]
    assert first["artifact_sha256"] != second["artifact_sha256"]


def test_extracted_source_payload_reproduces_fingerprint(tmp_path: Path):
    repo = _repo(tmp_path)
    result = release.build_release(repo, tmp_path / "out")
    extracted: list[release.SourceFile] = []
    with zipfile.ZipFile(result["artifact"]) as archive:
        for info in archive.infolist():
            relative = info.filename.removeprefix(release.ADDON_ID + "/")
            if relative != "build_manifest.json":
                extracted.append(release.SourceFile(relative, "100644", archive.read(info)))
    assert release.source_fingerprint(extracted) == result["source_fingerprint_sha256"]


@pytest.mark.parametrize("path", ["../escape", "/absolute", "dir\\windows"])
def test_unsafe_paths_are_rejected(path: str):
    with pytest.raises(release.PackagingError):
        release._validate_relative_path(path)


def test_symlinks_are_rejected(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "link").symlink_to("service.py")
    _run(repo, "add", "link")
    _run(repo, "commit", "-qm", "add symlink")
    commit = release.resolve_commit(repo, "HEAD")
    with pytest.raises(release.PackagingError, match="unsupported Git tree entry"):
        release.read_commit_files(repo, commit)


def test_case_fold_collisions_are_rejected(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "Name.py").write_text("A=1\n", encoding="utf-8")
    (repo / "name.py").write_text("A=2\n", encoding="utf-8")
    _run(repo, "add", "Name.py", "name.py")
    _run(repo, "commit", "-qm", "collision")
    with pytest.raises(release.PackagingError, match="case-fold"):
        release.read_commit_files(repo, release.resolve_commit(repo, "HEAD"))


def test_wrong_addon_identity_is_rejected(tmp_path: Path):
    repo = _repo(tmp_path, addon_id="service.other")
    with pytest.raises(release.PackagingError, match="addon id"):
        release.build_release(repo, tmp_path / "out")


def test_artifact_tampering_is_rejected(tmp_path: Path):
    repo = _repo(tmp_path)
    result = release.build_release(repo, tmp_path / "out")
    artifact = Path(result["artifact"])
    artifact.write_bytes(artifact.read_bytes() + b"tamper")
    with pytest.raises(release.PackagingError, match="size mismatch"):
        release.verify_release(repo, result["manifest"])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("artifact_sha256", "0" * 64, "artifact SHA-256 mismatch"),
        ("source_fingerprint_sha256", "0" * 64, "source fingerprint"),
        ("addon_id", "service.other", "addon id mismatch"),
        ("version", "9.9.9", "addon identity"),
    ],
)
def test_manifest_tampering_is_rejected(
    tmp_path: Path, field: str, value: str, message: str
):
    repo = _repo(tmp_path)
    result = release.build_release(repo, tmp_path / "out")
    payload = _manifest(Path(result["manifest"]))
    payload[field] = value
    _write_manifest(Path(result["manifest"]), payload)
    with pytest.raises(release.PackagingError, match=message):
        release.verify_release(repo, result["manifest"])


def test_manifest_cannot_claim_another_source_commit(tmp_path: Path):
    repo = _repo(tmp_path)
    first_commit = _run(repo, "rev-parse", "HEAD")
    (repo / "service.py").write_text("VALUE = 2\n", encoding="utf-8")
    _run(repo, "add", "service.py")
    _run(repo, "commit", "-qm", "second")
    result = release.build_release(repo, tmp_path / "out")
    payload = _manifest(Path(result["manifest"]))
    payload["source_git_sha"] = first_commit
    _write_manifest(Path(result["manifest"]), payload)
    with pytest.raises(release.PackagingError, match="source fingerprint"):
        release.verify_release(repo, result["manifest"])


def test_zip_payload_and_embedded_provenance_tampering_is_rejected(tmp_path: Path):
    repo = _repo(tmp_path)
    result = release.build_release(repo, tmp_path / "out")
    artifact = Path(result["artifact"])
    entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    with zipfile.ZipFile(artifact) as source:
        for info in source.infolist():
            data = source.read(info)
            if info.filename.endswith("/service.py"):
                data = b"CHANGED = True\n"
            entries.append((info, data))
    with zipfile.ZipFile(artifact, "w", zipfile.ZIP_STORED) as target:
        for info, data in entries:
            target.writestr(info, data)
    payload = _manifest(Path(result["manifest"]))
    payload["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    payload["artifact_size_bytes"] = artifact.stat().st_size
    _write_manifest(Path(result["manifest"]), payload)
    with pytest.raises(release.PackagingError, match="differs from source commit"):
        release.verify_release(repo, result["manifest"])


@pytest.mark.parametrize(
    "replacement",
    [
        {"builder_format_version": 1, "source_git_sha": "0" * 40, "source_fingerprint_sha256": "0" * 64},
        {"builder_format_version": 1, "source_git_sha": "1" * 40, "source_fingerprint_sha256": "1" * 64},
    ],
)
def test_embedded_provenance_tampering_is_rejected(tmp_path: Path, replacement: dict):
    repo = _repo(tmp_path)
    result = release.build_release(repo, tmp_path / "out")
    artifact = Path(result["artifact"])
    entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    with zipfile.ZipFile(artifact) as source:
        for info in source.infolist():
            data = source.read(info)
            if info.filename.endswith("/build_manifest.json"):
                data = (json.dumps(replacement, indent=2, sort_keys=True) + "\n").encode()
            entries.append((info, data))
    with zipfile.ZipFile(artifact, "w", zipfile.ZIP_STORED) as target:
        for info, data in entries:
            target.writestr(info, data)
    payload = _manifest(Path(result["manifest"]))
    payload["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    payload["artifact_size_bytes"] = artifact.stat().st_size
    _write_manifest(Path(result["manifest"]), payload)
    with pytest.raises(release.PackagingError, match="embedded build provenance"):
        release.verify_release(repo, result["manifest"])


def test_addon_version_mismatch_inside_zip_is_rejected(tmp_path: Path):
    repo = _repo(tmp_path)
    result = release.build_release(repo, tmp_path / "out")
    manifest_path = Path(result["manifest"])
    payload = _manifest(manifest_path)
    payload["version"] = "9.9.9"
    _write_manifest(manifest_path, payload)
    with pytest.raises(release.PackagingError, match="addon identity"):
        release.verify_release(repo, manifest_path)
