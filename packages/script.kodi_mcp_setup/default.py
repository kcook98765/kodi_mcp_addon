# -*- coding: utf-8 -*-
"""User-facing setup helper for Kodi MCP."""

import json
import os
import time
import urllib.error
import urllib.request

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs


ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
REPOSITORY_ADDON_ID = "repository.kodi-mcp"
REPOSITORY_NAME = "Kodi MCP Repository"
BRIDGE_STATE_URL = "http://127.0.0.1:8765/mcp/state"
DOWNLOAD_FILENAME = "repository.kodi-mcp-latest.zip"


def _log(message):
    xbmc.log("[%s] %s" % (ADDON_ID, message), xbmc.LOGINFO)


def _notify(message, level=xbmcgui.NOTIFICATION_INFO):
    xbmcgui.Dialog().notification("Kodi MCP Setup", message, level, 5000)


def _profile_dir():
    path = xbmcvfs.translatePath("special://profile/addon_data/%s" % ADDON_ID)
    if not xbmcvfs.exists(path):
        xbmcvfs.mkdirs(path)
    return path


def _preferred_download_dir():
    home_downloads = os.path.expanduser("~/Downloads")
    if home_downloads and os.path.isdir(home_downloads):
        path = os.path.join(home_downloads, "Kodi MCP")
        if not xbmcvfs.exists(path):
            xbmcvfs.mkdirs(path)
        return path
    return _profile_dir()


def _download_path():
    return os.path.join(_preferred_download_dir(), DOWNLOAD_FILENAME)


def _http_json(url, timeout=5):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _download(url, path, timeout=30):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read()
    parent = os.path.dirname(path)
    if parent and not xbmcvfs.exists(parent):
        xbmcvfs.mkdirs(parent)
    with open(path, "wb") as handle:
        handle.write(data)
    return len(data)


def _server_url_from_bridge():
    try:
        state = _http_json(BRIDGE_STATE_URL, timeout=3)
    except Exception:
        return None
    result = state.get("result") if isinstance(state, dict) else None
    state_obj = result.get("state") if isinstance(result, dict) else None
    registration = state_obj.get("registration") if isinstance(state_obj, dict) else None
    if not isinstance(registration, dict):
        return None
    value = str(registration.get("server_base_url") or "").strip()
    return value.rstrip("/") or None


def _configured_server_url():
    configured = str(ADDON.getSetting("mcp_server_url") or "").strip().rstrip("/")
    return configured or _server_url_from_bridge()


def _set_server_url():
    current = _configured_server_url() or "http://claw.home.arpa:8010"
    keyboard = xbmc.Keyboard(current, "MCP server URL")
    keyboard.doModal()
    if not keyboard.isConfirmed():
        return None
    value = keyboard.getText().strip().rstrip("/")
    if value:
        ADDON.setSetting("mcp_server_url", value)
        _notify("Server URL saved")
    return value or None


def _repo_installed():
    return bool(xbmc.getCondVisibility("System.HasAddon(%s)" % REPOSITORY_ADDON_ID))


def _server_status(server_url):
    if not server_url:
        return {"ok": False, "error": "MCP server URL is not set"}
    try:
        health = _http_json("%s/health" % server_url, timeout=5)
        repo_info = _http_json("%s/repo/info" % server_url, timeout=5)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "server_url": server_url}
    install_urls = repo_info.get("install_urls") if isinstance(repo_info, dict) else None
    zip_url = install_urls.get("repository_addon_zip") if isinstance(install_urls, dict) else None
    return {
        "ok": True,
        "server_url": server_url,
        "health": health,
        "repository_addon_zip_url": zip_url,
    }


def _status_lines():
    server_url = _configured_server_url()
    server = _server_status(server_url)
    zip_path = _download_path()
    lines = [
        "MCP server: %s" % (server_url or "not set"),
        "Server reachable: %s" % ("yes" if server.get("ok") else "no"),
        "%s installed: %s" % (REPOSITORY_NAME, "yes" if _repo_installed() else "no"),
        "Prepared zip: %s" % ("yes" if xbmcvfs.exists(zip_path) else "no"),
    ]
    if server.get("error"):
        lines.append("Server error: %s" % server.get("error"))
    return lines, server


def show_status():
    lines, _ = _status_lines()
    xbmcgui.Dialog().ok("Kodi MCP Setup", "\n".join(lines))


def prepare_repository_zip():
    server_url = _configured_server_url()
    if not server_url:
        server_url = _set_server_url()
    server = _server_status(server_url)
    if not server.get("ok"):
        xbmcgui.Dialog().ok("Kodi MCP Setup", "MCP server is not reachable.\n\n%s" % server.get("error"))
        return None
    zip_url = server.get("repository_addon_zip_url")
    if not zip_url:
        xbmcgui.Dialog().ok("Kodi MCP Setup", "MCP server did not report a repository add-on zip URL.")
        return None

    path = _download_path()
    try:
        size = _download(zip_url, path, timeout=30)
    except Exception as exc:
        xbmcgui.Dialog().ok("Kodi MCP Setup", "Failed to download repository add-on zip.\n\n%s" % exc)
        return None

    _log("prepared repository addon zip path=%s size=%s" % (path, size))
    _notify("Repository add-on zip prepared")
    return path


def open_install_from_zip():
    path = _download_path()
    if not xbmcvfs.exists(path):
        prepared = prepare_repository_zip()
        if not prepared:
            return
        path = prepared

    xbmcgui.Dialog().ok(
        "Kodi MCP Setup",
        "Kodi will open the Add-on browser.\n\nChoose Install from zip file, then select:\n%s" % path,
    )
    xbmc.executebuiltin("ActivateWindow(AddonBrowser)")


def open_settings():
    ADDON.openSettings()


def setup_menu():
    while True:
        lines, server = _status_lines()
        repo_ready = _repo_installed()
        title = "Kodi MCP Setup"
        options = [
            "Show status",
            "Set MCP server URL",
            "Prepare repository add-on zip",
            "Open Install from zip file",
            "Open setup settings",
        ]
        if repo_ready:
            options.insert(0, "Ready: %s is installed" % REPOSITORY_NAME)
        elif server.get("ok"):
            options.insert(0, "Next: prepare repository add-on zip")
        else:
            options.insert(0, "Next: set or start MCP server")

        choice = xbmcgui.Dialog().select(title, options)
        if choice < 0:
            return
        selected = options[choice]
        if selected.startswith("Ready:"):
            xbmcgui.Dialog().ok(title, "\n".join(lines))
        elif selected.startswith("Next: set"):
            _set_server_url()
        elif selected.startswith("Next: prepare"):
            prepare_repository_zip()
        elif selected == "Show status":
            show_status()
        elif selected == "Set MCP server URL":
            _set_server_url()
        elif selected == "Prepare repository add-on zip":
            prepare_repository_zip()
        elif selected == "Open Install from zip file":
            open_install_from_zip()
            return
        elif selected == "Open setup settings":
            open_settings()
            # Give Kodi a moment to persist any setting before refreshing status.
            time.sleep(0.5)


def main():
    try:
        setup_menu()
    except Exception as exc:
        _log("setup failed: %s" % exc)
        xbmcgui.Dialog().ok("Kodi MCP Setup", "Setup failed.\n\n%s" % exc)


if __name__ == "__main__":
    main()
