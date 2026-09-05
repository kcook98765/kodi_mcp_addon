from __future__ import annotations

import hashlib
import importlib
import os
import sys
import types
import zipfile
from pathlib import Path

import pytest


def _bridge_module():
    xbmc = sys.modules.setdefault("xbmc", types.ModuleType("xbmc"))
    xbmc.log = lambda *args, **kwargs: None
    xbmc.LOGDEBUG = 0
    xbmc.LOGINFO = 1
    xbmc.LOGWARNING = 2
    xbmc.LOGERROR = 3
    xbmc.executebuiltin = lambda *args, **kwargs: None

    xbmcaddon = sys.modules.setdefault("xbmcaddon", types.ModuleType("xbmcaddon"))
    xbmcaddon.Addon = lambda *args, **kwargs: None
    sys.modules.setdefault("xbmcgui", types.ModuleType("xbmcgui"))
    xbmcvfs = sys.modules.setdefault("xbmcvfs", types.ModuleType("xbmcvfs"))
    xbmcvfs.translatePath = lambda path: path
    xbmcvfs.exists = os.path.exists
    xbmcvfs.mkdirs = lambda path: os.makedirs(path, exist_ok=True) or True
    return importlib.import_module("http_bridge")


def _write_zip(path: Path, *, addon_id="repository.kodi-mcp", version="1.0.4", extra=False):
    addon_xml = (
        '<addon id="%s" version="%s">'
        '<extension point="xbmc.addon.repository" /></addon>' % (addon_id, version)
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("repository.kodi-mcp/addon.xml", addon_xml)
        archive.writestr("repository.kodi-mcp/service.py", "")
        archive.writestr("repository.kodi-mcp/addons.xml", "<addons />")
        if extra:
            archive.writestr("repository.kodi-mcp/extra", "")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _state(bridge, path: Path, digest: str, *, version="1.0.4"):
    return {
        "repo_zip": {
            "repo_id": bridge.REPOSITORY_BOOTSTRAP_REPO_ID,
            "repo_version": version,
            "special_path": bridge.REPOSITORY_BOOTSTRAP_SPECIAL_PATH,
            "size_bytes": path.stat().st_size if path.exists() else 1,
            "sha256": digest,
        }
    }


def test_canonical_repository_accepted(tmp_path: Path, monkeypatch):
    bridge = _bridge_module()
    path = tmp_path / "dev-repo.zip"
    digest = _write_zip(path)
    monkeypatch.setattr(bridge, "_translate", lambda value: str(path))
    result = bridge.validate_repository_bootstrap(_state(bridge, path, digest))
    assert result["addon_id"] == "repository.kodi-mcp"
    assert result["version"] == "1.0.4"
    assert result["sha256"] == digest


@pytest.mark.parametrize("kind", ["wrong_id", "extra", "sha", "missing"])
def test_noncanonical_repository_rejected(tmp_path: Path, monkeypatch, kind: str):
    bridge = _bridge_module()
    path = tmp_path / "dev-repo.zip"
    if kind != "missing":
        digest = _write_zip(path, addon_id="repository.other" if kind == "wrong_id" else "repository.kodi-mcp", extra=kind == "extra")
    else:
        digest = "0" * 64
    monkeypatch.setattr(bridge, "_translate", lambda value: str(path))
    state = _state(bridge, path, "f" * 64 if kind == "sha" else digest)
    with pytest.raises(ValueError):
        bridge.validate_repository_bootstrap(state)


def test_staged_metadata_and_zip_version_must_match(tmp_path: Path, monkeypatch):
    bridge = _bridge_module()
    path = tmp_path / "dev-repo.zip"
    digest = _write_zip(path, version="1.0.4")
    monkeypatch.setattr(bridge, "_translate", lambda value: str(path))

    with pytest.raises(ValueError, match="does not match staged metadata"):
        bridge.validate_repository_bootstrap(_state(bridge, path, digest, version="1.0.5"))


def test_already_installed_is_idempotent(tmp_path: Path, monkeypatch):
    bridge = _bridge_module()
    path = tmp_path / "dev-repo.zip"
    digest = _write_zip(path)
    monkeypatch.setattr(bridge, "_translate", lambda value: str(path))
    monkeypatch.setattr(bridge, "load_state", lambda: _state(bridge, path, digest))
    handler = object.__new__(bridge.KodiBridgeHandler)
    monkeypatch.setattr(
        handler,
        "_get_addon_info",
        lambda addon_id: ({"addon_id": addon_id, "installed": True, "enabled": True, "version": "1.0.4"}, 200),
    )
    result, status = handler._install_repository_bootstrap()
    assert status == 200
    assert result["ok"] is True
    assert result["action"] == "already_installed"


def test_absent_repository_is_installed(tmp_path: Path, monkeypatch):
    bridge = _bridge_module()
    path = tmp_path / "dev-repo.zip"
    digest = _write_zip(path)
    destination = tmp_path / "addons" / "repository.kodi-mcp"

    def translate(value):
        if value == bridge.REPOSITORY_BOOTSTRAP_SPECIAL_PATH:
            return str(path)
        if value == bridge.REPOSITORY_BOOTSTRAP_DEST_SPECIAL:
            return str(destination)
        return value

    observations = [
        {"addon_id": "repository.kodi-mcp", "installed": False, "enabled": False, "version": None},
        {"addon_id": "repository.kodi-mcp", "installed": True, "enabled": True, "version": "1.0.4"},
        {"addon_id": "repository.kodi-mcp", "installed": True, "enabled": True, "version": "1.0.4"},
    ]
    monkeypatch.setattr(bridge, "_translate", translate)
    monkeypatch.setattr(bridge, "load_state", lambda: _state(bridge, path, digest))
    handler = object.__new__(bridge.KodiBridgeHandler)
    monkeypatch.setattr(handler, "_get_addon_info", lambda addon_id: (observations.pop(0), 200))
    monkeypatch.setattr(handler, "_jsonrpc", lambda *args, **kwargs: ({"result": "OK"}, None))

    result, status = handler._install_repository_bootstrap()

    assert status == 200
    assert result["action"] == "installed"
    assert result["version"] == "1.0.4"
    assert (destination / "addon.xml").is_file()


def test_older_installed_repository_is_upgraded(tmp_path: Path, monkeypatch):
    bridge = _bridge_module()
    path = tmp_path / "dev-repo.zip"
    digest = _write_zip(path)
    destination = tmp_path / "addons" / "repository.kodi-mcp"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("old", encoding="utf-8")

    def translate(value):
        if value == bridge.REPOSITORY_BOOTSTRAP_SPECIAL_PATH:
            return str(path)
        if value == bridge.REPOSITORY_BOOTSTRAP_DEST_SPECIAL:
            return str(destination)
        return value

    observations = [
        {"addon_id": "repository.kodi-mcp", "installed": True, "enabled": True, "version": "1.0.2"},
        {"addon_id": "repository.kodi-mcp", "installed": True, "enabled": True, "version": "1.0.4"},
        {"addon_id": "repository.kodi-mcp", "installed": True, "enabled": True, "version": "1.0.4"},
    ]
    monkeypatch.setattr(bridge, "_translate", translate)
    monkeypatch.setattr(bridge, "load_state", lambda: _state(bridge, path, digest))
    handler = object.__new__(bridge.KodiBridgeHandler)
    monkeypatch.setattr(handler, "_get_addon_info", lambda addon_id: (observations.pop(0), 200))
    monkeypatch.setattr(handler, "_jsonrpc", lambda *args, **kwargs: ({"result": "OK"}, None))

    result, status = handler._install_repository_bootstrap()

    assert status == 200
    assert result["action"] == "upgraded"
    assert result["version"] == "1.0.4"
    assert (destination / "addon.xml").is_file()
    assert not Path(str(destination) + ".previous").exists()


def test_newer_installed_repository_is_not_downgraded(tmp_path: Path, monkeypatch):
    bridge = _bridge_module()
    path = tmp_path / "dev-repo.zip"
    digest = _write_zip(path)
    monkeypatch.setattr(bridge, "_translate", lambda value: str(path))
    monkeypatch.setattr(bridge, "load_state", lambda: _state(bridge, path, digest))
    handler = object.__new__(bridge.KodiBridgeHandler)
    monkeypatch.setattr(
        handler,
        "_get_addon_info",
        lambda addon_id: ({"addon_id": addon_id, "installed": True, "enabled": True, "version": "1.0.5"}, 200),
    )

    result, status = handler._install_repository_bootstrap()

    assert status == 409
    assert result["error_code"] == "DOWNGRADE_FORBIDDEN"


def test_bridge_primitive_has_no_caller_deployment_identity():
    bridge = _bridge_module()
    import inspect

    assert list(inspect.signature(bridge.KodiBridgeHandler._install_repository_bootstrap).parameters) == ["self"]


def test_bridge_has_no_independent_repository_version_authority():
    bridge = _bridge_module()

    assert not hasattr(bridge, "REPOSITORY_BOOTSTRAP_VERSION")
