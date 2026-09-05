from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from xml.etree import ElementTree


def test_declared_addon_version_is_0_2_40():
    root = ElementTree.parse(Path(__file__).parents[1] / "addon.xml").getroot()
    assert root.attrib["id"] == "service.kodi_mcp"
    assert root.attrib["version"] == "0.2.40"


def _install_kodi_stubs():
    xbmc = types.ModuleType("xbmc")
    setattr(xbmc, "log", lambda *args, **kwargs: None)
    setattr(xbmc, "LOGDEBUG", 0)
    setattr(xbmc, "LOGINFO", 1)
    setattr(xbmc, "LOGWARNING", 2)
    setattr(xbmc, "LOGERROR", 3)

    xbmcaddon = types.ModuleType("xbmcaddon")
    setattr(xbmcaddon, "Addon", lambda *args, **kwargs: None)
    xbmcgui = types.ModuleType("xbmcgui")
    xbmcvfs = types.ModuleType("xbmcvfs")
    setattr(xbmcvfs, "translatePath", lambda path: path)

    sys.modules.update(
        xbmc=xbmc,
        xbmcaddon=xbmcaddon,
        xbmcgui=xbmcgui,
        xbmcvfs=xbmcvfs,
    )


def test_deep_health_is_public_and_proves_kodi_jsonrpc_reachability(monkeypatch):
    _install_kodi_stubs()
    bridge = importlib.import_module("http_bridge")
    monkeypatch.setattr(bridge, "BRIDGE_START_TIME", 90)
    monkeypatch.setattr(bridge.time, "time", lambda: 100)
    handler = object.__new__(bridge.KodiBridgeHandler)

    class Addon:
        def getAddonInfo(self, key):
            return {"id": "service.kodi_mcp", "version": "0.2.34"}[key]

    monkeypatch.setattr(handler, "_get_addon", lambda: Addon())
    monkeypatch.setattr(
        handler,
        "_jsonrpc",
        lambda method, params=None: ({"jsonrpc": "2.0", "id": "test", "result": "pong"}, None),
    )
    monkeypatch.setattr(
        handler,
        "_get_build_identity",
        lambda: {"source_git_sha": "abc1234", "source_fingerprint_sha256": "f" * 64},
        raising=False,
    )

    payload, status = handler._get_deep_health()
    shallow = handler._get_shallow_health()

    assert handler._is_public_get_path("/health/deep") is True
    assert status == 200
    assert shallow == {
        "status": "ok",
        "service": "service.kodi_mcp",
        "addon_id": "service.kodi_mcp",
        "version": "0.2.34",
        "build": {
            "source_git_sha": "abc1234",
            "source_fingerprint_sha256": "f" * 64,
        },
        "health_type": "shallow",
        "uptime_seconds": 10,
    }
    assert payload == {
        "status": "ok",
        "service": "service.kodi_mcp",
        "addon_id": "service.kodi_mcp",
        "version": "0.2.34",
        "build": {
            "source_git_sha": "abc1234",
            "source_fingerprint_sha256": "f" * 64,
        },
        "health_type": "deep",
        "uptime_seconds": 10,
        "kodi_jsonrpc_ok": True,
    }
