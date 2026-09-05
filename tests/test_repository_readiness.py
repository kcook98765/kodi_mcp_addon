from __future__ import annotations

import hashlib
import importlib
import inspect
import os
import sqlite3
import sys
import types
from pathlib import Path

import pytest


def _bridge_module():
    xbmc = sys.modules.setdefault("xbmc", types.ModuleType("xbmc"))
    xbmc.log = lambda *args, **kwargs: None
    xbmc.LOGDEBUG = 0
    xbmc.LOGINFO = 1
    xbmc.LOGWARNING = 2
    xbmc.LOGERROR = 3
    xbmc.getCondVisibility = lambda value: False
    xbmcaddon = sys.modules.setdefault("xbmcaddon", types.ModuleType("xbmcaddon"))
    xbmcaddon.Addon = lambda *args, **kwargs: None
    sys.modules.setdefault("xbmcgui", types.ModuleType("xbmcgui"))
    xbmcvfs = sys.modules.setdefault("xbmcvfs", types.ModuleType("xbmcvfs"))
    xbmcvfs.translatePath = lambda path: path
    xbmcvfs.exists = os.path.exists
    xbmcvfs.File = lambda path, *args: open(path, "rb")
    return importlib.import_module("http_bridge")


def _addon_xml(version="1.0.4"):
    return """<addon id="repository.kodi-mcp" version="%s">
      <extension point="xbmc.addon.repository"><dir>
        <info>http://repo.test/repo/content/addons.xml</info>
        <checksum>http://repo.test/repo/content/addons.xml.md5</checksum>
        <datadir zip="true">http://repo.test/repo/content/zips/</datadir>
      </dir></extension>
    </addon>""" % version


def _handler(tmp_path: Path, monkeypatch, *, installed=True, enabled=True, version="1.0.4"):
    bridge = _bridge_module()
    addon_dir = tmp_path / "repository.kodi-mcp"
    addon_dir.mkdir()
    (addon_dir / "addon.xml").write_text(_addon_xml(version), encoding="utf-8")
    handler = object.__new__(bridge.KodiBridgeHandler)
    monkeypatch.setattr(
        handler,
        "_get_addon_info",
        lambda addon_id: (
            {
                "addon_id": addon_id,
                "installed": installed,
                "enabled": enabled if installed else False,
                "version": version if installed else None,
                "install_path": str(addon_dir) if installed else None,
            },
            200,
        ),
    )
    return bridge, handler


def _successful_fetches(bridge, metadata=None):
    metadata = metadata or b'<addons><addon id="service.kodi_mcp" version="0.2.34" /></addons>'
    checksum = hashlib.md5(metadata).hexdigest().encode("ascii") + b"  addons.xml\n"

    def fetch(url, max_bytes, first_byte_only=False):
        if url.endswith("addons.xml"):
            return {"reachable": True, "http_status": 200, "body": metadata}
        if url.endswith("addons.xml.md5"):
            return {"reachable": True, "http_status": 200, "body": checksum}
        return {"reachable": True, "http_status": 206, "body": b"P"}

    return fetch


def test_repository_absent_is_reported_without_probing(tmp_path, monkeypatch):
    bridge, handler = _handler(tmp_path, monkeypatch, installed=False)
    monkeypatch.setattr(handler, "_fetch_repository_url", lambda *args, **kwargs: pytest.fail("probe"))
    result, status = handler._repository_readiness()
    assert status == 200
    assert result["installed"] is False
    assert result["metadata"]["reachable"] is False
    assert result["catalog_ingestion"] == {
        "observable": False,
        "state": "unknown",
        "reason": "Kodi exposes no stable read-only repository catalog-ingestion API",
    }


def test_disabled_repository_is_reported(tmp_path, monkeypatch):
    bridge, handler = _handler(tmp_path, monkeypatch, enabled=False)
    monkeypatch.setattr(handler, "_fetch_repository_url", _successful_fetches(bridge))
    result, _ = handler._repository_readiness()
    assert result["installed"] is True
    assert result["enabled"] is False


def test_valid_repository_proves_metadata_checksum_and_package(tmp_path, monkeypatch):
    bridge, handler = _handler(tmp_path, monkeypatch)
    monkeypatch.setattr(handler, "_fetch_repository_url", _successful_fetches(bridge))
    result, _ = handler._repository_readiness()
    assert result["configured_identity"] == {"addon_id": "repository.kodi-mcp", "version": "1.0.4"}
    assert result["metadata"]["parseable"] is True
    assert result["metadata"]["addon_count"] == 1
    assert result["checksum"]["match"] is True
    assert result["package"]["reachable"] is True
    assert result["package"]["probe_url"].endswith(
        "/service.kodi_mcp/service.kodi_mcp-0.2.34.zip"
    )


@pytest.mark.parametrize(
    ("failure", "field"),
    [
        ("metadata", "metadata"),
        ("checksum", "checksum"),
        ("package", "package"),
    ],
)
def test_unreachable_repository_resources_are_reported(tmp_path, monkeypatch, failure, field):
    bridge, handler = _handler(tmp_path, monkeypatch)
    success = _successful_fetches(bridge)

    def fetch(url, max_bytes, first_byte_only=False):
        if failure == "metadata" and url.endswith("addons.xml"):
            return {"reachable": False, "http_status": 503}
        if failure == "checksum" and url.endswith("addons.xml.md5"):
            return {"reachable": False, "http_status": 503}
        if failure == "package" and url.endswith(".zip"):
            return {"reachable": False, "http_status": 404}
        return success(url, max_bytes, first_byte_only)

    monkeypatch.setattr(handler, "_fetch_repository_url", fetch)
    result, _ = handler._repository_readiness()
    assert result[field]["reachable"] is False


def test_checksum_mismatch_is_reported(tmp_path, monkeypatch):
    bridge, handler = _handler(tmp_path, monkeypatch)
    success = _successful_fetches(bridge)

    def fetch(url, max_bytes, first_byte_only=False):
        if url.endswith("addons.xml.md5"):
            return {"reachable": True, "http_status": 200, "body": b"0" * 32}
        return success(url, max_bytes, first_byte_only)

    monkeypatch.setattr(handler, "_fetch_repository_url", fetch)
    result, _ = handler._repository_readiness()
    assert result["checksum"]["match"] is False


def test_malformed_addons_xml_is_reported(tmp_path, monkeypatch):
    bridge, handler = _handler(tmp_path, monkeypatch)
    monkeypatch.setattr(handler, "_fetch_repository_url", _successful_fetches(bridge, b"<not-addons />"))
    result, _ = handler._repository_readiness()
    assert result["metadata"]["reachable"] is True
    assert result["metadata"]["parseable"] is False
    assert result["package"]["observable"] is False


def test_bridge_readiness_has_no_caller_controlled_identity():
    bridge = _bridge_module()
    assert list(inspect.signature(bridge.KodiBridgeHandler._repository_readiness).parameters) == ["self"]
    assert list(inspect.signature(bridge.KodiBridgeHandler._fetch_repository_url).parameters) == [
        "self", "url", "max_bytes", "first_byte_only"
    ]


def test_bridge_endpoint_rejects_query_arguments(monkeypatch):
    bridge = _bridge_module()
    handler = object.__new__(bridge.KodiBridgeHandler)
    handler.path = "/repo/readiness?url=https://example.invalid&addon_id=repository.other"
    captured = {}
    monkeypatch.setattr(handler, "_require_token_auth", lambda: True)
    monkeypatch.setattr(
        handler,
        "_write_envelope",
        lambda payload, status=200: captured.update(payload=payload, status=status),
    )
    monkeypatch.setattr(handler, "_repository_readiness", lambda: pytest.fail("probe"))
    handler.do_GET()
    assert captured == {
        "payload": {"ok": False, "error_code": "ARGUMENTS_FORBIDDEN"},
        "status": 400,
    }


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://user:secret@repo.test/addons.xml"])
def test_repository_probe_rejects_non_http_and_embedded_credentials(url):
    bridge = _bridge_module()
    handler = object.__new__(bridge.KodiBridgeHandler)
    result = handler._fetch_repository_url(url, 100)
    assert result["reachable"] is False


def test_catalog_database_evidence_is_read_only_and_truthfully_bounded(tmp_path, monkeypatch):
    bridge = _bridge_module()
    db_path = tmp_path / "Addons33.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        "CREATE TABLE repo (id integer primary key, addonID text, checksum text, "
        "lastcheck text, version text, nextcheck text);"
        "CREATE TABLE addons (id integer primary key, addonID text, version text);"
        "CREATE TABLE addonlinkrepo (idRepo integer, idAddon integer);"
        "INSERT INTO repo VALUES (1, 'repository.kodi-mcp', 'abc', "
        "'2026-09-04 12:00:00', '1.0.4', '2026-09-05 12:00:00');"
        "INSERT INTO addons VALUES (2, 'service.kodi_mcp', '0.2.34');"
        "INSERT INTO addonlinkrepo VALUES (1, 2);"
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(bridge.xbmcvfs, "translatePath", lambda value: str(tmp_path / "Addons*.db"))
    handler = object.__new__(bridge.KodiBridgeHandler)
    refresh, ingestion = handler._repository_catalog_evidence()
    assert refresh["state"] == "repository_record_observed"
    assert refresh["last_check"] == "2026-09-04 12:00:00"
    assert refresh["completion_proven"] is False
    assert ingestion == {
        "observable": True,
        "state": "entries_observed",
        "evidence_source": "kodi_addons_database_internal",
        "entry_count": 1,
        "freshness_proven": False,
    }
