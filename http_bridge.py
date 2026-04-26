# -*- coding: utf-8 -*-
"""Minimal local HTTP bridge for Kodi MCP development.

Milestone A additions:
- Shared-token authenticated MCP registration + state endpoints
- Repo zip staging (server -> addon local path)
- Minimal persisted state in addon_data/service.kodi_mcp/state.json
"""

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import xbmc
import xbmcaddon
import xbmcvfs

BRIDGE_BIND_HOST = "0.0.0.0"
BRIDGE_BIND_PORT = 8765
BRIDGE_START_TIME = time.time()
MAX_FILE_READ_BYTES = 16384
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# Milestone A: repo zip staging defaults
AUTH_HEADER_TOKEN = "X-Kodi-MCP-Token"
STATE_SCHEMA_VERSION = 1
STATE_SPECIAL_PATH = "special://profile/addon_data/service.kodi_mcp/state.json"
DEV_REPO_DIR_SPECIAL = "special://profile/addon_data/service.kodi_mcp/dev_repo"
MAX_REPO_ZIP_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MiB
UPLOAD_CHUNK_SIZE = 64 * 1024
REPO_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
GUI_ACTIONS = {
    "up": "Input.Up",
    "down": "Input.Down",
    "left": "Input.Left",
    "right": "Input.Right",
    "select": "Input.Select",
    "back": "Input.Back",
    "home": "Input.Home",
    "context": "Input.ContextMenu",
    "info": "Input.Info",
}


def _now_epoch_seconds():
    return int(time.time())


def _translate(special_path):
    return xbmcvfs.translatePath(special_path)


def load_state():
    """Load persisted state.json (or return an empty initialized state)."""

    translated = _translate(STATE_SPECIAL_PATH)
    if not xbmcvfs.exists(translated):
        return {"schema_version": STATE_SCHEMA_VERSION, "state_rev": 0}

    handle = xbmcvfs.File(translated)
    try:
        raw = handle.read()
    finally:
        handle.close()

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")

    try:
        state = json.loads(raw or "{}")
    except Exception:
        # If state is corrupted, fall back to empty state rather than hard-failing.
        state = {}

    if not isinstance(state, dict):
        state = {}

    state.setdefault("schema_version", STATE_SCHEMA_VERSION)
    state.setdefault("state_rev", 0)
    return state


def save_state(state):
    """Persist state.json; increments state_rev."""

    if not isinstance(state, dict):
        state = {"schema_version": STATE_SCHEMA_VERSION, "state_rev": 0}

    state["schema_version"] = STATE_SCHEMA_VERSION
    state["state_rev"] = int(state.get("state_rev") or 0) + 1

    translated = _translate(STATE_SPECIAL_PATH)
    parent_dir = os.path.dirname(translated)
    if parent_dir and not xbmcvfs.exists(parent_dir):
        xbmcvfs.mkdirs(parent_dir)

    tmp_path = translated + ".tmp"
    body = json.dumps(state, indent=2, sort_keys=True).encode("utf-8")

    handle = xbmcvfs.File(tmp_path, "wb")
    try:
        handle.write(body)
    finally:
        handle.close()

    if xbmcvfs.exists(translated):
        xbmcvfs.delete(translated)
    xbmcvfs.rename(tmp_path, translated)
    return state


def compute_derived_state(state):
    """Compute addon-side derived state for both HTTP responses and UI.

    Returns a dict with:
        now, registration_present, registration_age_seconds, registration_stale,
        expires_at, repo_zip_present_in_state, repo_zip_file_exists,
        dev_setup_available
    """

    now = _now_epoch_seconds()
    reg = (state or {}).get("registration")
    repo_zip = (state or {}).get("repo_zip")

    registration_present = isinstance(reg, dict)
    repo_zip_present_in_state = isinstance(repo_zip, dict)

    if registration_present:
        last_seen = int(reg.get("last_seen_at") or 0)
        ttl = int(reg.get("applied_ttl_seconds") or 0)
        expires_at = last_seen + ttl
        registration_age_seconds = max(0, now - last_seen)
        registration_stale = now > expires_at
    else:
        expires_at = None
        registration_age_seconds = None
        registration_stale = True

    if repo_zip_present_in_state:
        special_path = str(repo_zip.get("special_path") or "").strip()
        translated = _translate(special_path) if special_path else ""
        repo_zip_file_exists = bool(translated and xbmcvfs.exists(translated))
    else:
        repo_zip_file_exists = False

    features_ok = False
    if registration_present:
        features = reg.get("features") or {}
        if isinstance(features, dict):
            features_ok = bool(features.get("repo_zip_staging"))

    dev_setup_available = (
        registration_present
        and not registration_stale
        and features_ok
        and repo_zip_present_in_state
        and repo_zip_file_exists
    )

    return {
        "now": now,
        "registration_present": registration_present,
        "registration_age_seconds": registration_age_seconds,
        "registration_stale": registration_stale,
        "expires_at": expires_at,
        "repo_zip_present_in_state": repo_zip_present_in_state,
        "repo_zip_file_exists": repo_zip_file_exists,
        "dev_setup_available": dev_setup_available,
    }


def get_ui_dev_setup_state():
    """Small helper for UI scripts.

    Returns:
        {
          registration_present,
          registration_stale,
          repo_zip_present_in_state,
          repo_zip_file_exists,
          dev_setup_available,
          repo_zip_special_path,
          missing_conditions: [..]
        }
    """

    state = load_state()
    derived = compute_derived_state(state)
    repo_zip = (state or {}).get("repo_zip") or {}
    repo_zip_special_path = str(repo_zip.get("special_path") or "").strip() or None

    missing = []
    if not derived.get("registration_present"):
        missing.append("no active MCP registration")
    elif derived.get("registration_stale"):
        missing.append("registration stale")

    if not derived.get("repo_zip_present_in_state"):
        missing.append("no staged repo zip metadata")
    elif not derived.get("repo_zip_file_exists"):
        missing.append("staged repo zip file missing")

    return {
        "registration_present": bool(derived.get("registration_present")),
        "registration_stale": bool(derived.get("registration_stale")),
        "repo_zip_present_in_state": bool(derived.get("repo_zip_present_in_state")),
        "repo_zip_file_exists": bool(derived.get("repo_zip_file_exists")),
        "dev_setup_available": bool(derived.get("dev_setup_available")),
        "repo_zip_special_path": repo_zip_special_path,
        "missing_conditions": missing,
    }


class KodiBridgeHandler(BaseHTTPRequestHandler):
    """Request handler for the minimal Kodi bridge."""

    server_version = "KodiMCPBridge/0.1"

    def log_message(self, format, *args):
        xbmc.log("[service.kodi_mcp] " + (format % args), xbmc.LOGDEBUG)

    def _write_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_envelope(self, result, status=200):
        """Write a Milestone A envelope response."""

        payload = {
            "transport": {"ok": True},
            "result": result,
        }
        self._write_json(payload, status=status)

    def _require_token_auth(self):
        """Validate X-Kodi-MCP-Token against addon setting mcp_token."""

        addon = self._get_addon()
        configured = str(addon.getSetting("mcp_token") or "").strip()
        provided = str(self.headers.get(AUTH_HEADER_TOKEN) or "").strip()

        if not configured:
            return True

        if not provided or not hmac.compare_digest(configured, provided):
            self._write_envelope(
                {
                    "ok": False,
                    "error_code": "UNAUTHORIZED",
                    "message": "Missing or invalid %s" % AUTH_HEADER_TOKEN,
                },
                status=401,
            )
            return False

        return True

    def _is_public_get_path(self, path):
        return path in ("/health", "/status", "/runtime/info", "/capabilities", "/control/capabilities")

    def _read_json_body(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            return json.loads(body.decode("utf-8")), None
        except Exception:
            return None, "invalid json body"

    def _jsonrpc(self, method, params=None):
        payload = {
            "jsonrpc": "2.0",
            "id": "bridge-%s" % int(time.time() * 1000),
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        raw = xbmc.executeJSONRPC(json.dumps(payload))
        try:
            return json.loads(raw), None
        except Exception as exc:
            return None, "invalid json-rpc response: %s" % exc

    def _gui_action(self, action):
        action = str(action or "").strip().lower()
        method = GUI_ACTIONS.get(action)
        if not method:
            return {
                "error": "unsupported gui action",
                "action": action,
                "allowed": sorted(GUI_ACTIONS.keys()),
            }, 400

        response, error = self._jsonrpc(method)
        if error:
            return {"error": error, "action": action, "method": method}, 500
        if isinstance(response, dict) and response.get("error"):
            return {
                "error": response.get("error"),
                "action": action,
                "method": method,
                "jsonrpc": response,
            }, 500

        return {"ok": True, "action": action, "method": method, "jsonrpc": response}, 200

    def _compute_derived_state(self, state):
        # Backwards compatible method wrapper.
        return compute_derived_state(state)

    def _get_addon(self):
        return xbmcaddon.Addon()

    def _get_addon_install_path(self):
        addon = self._get_addon()
        return xbmcvfs.translatePath(addon.getAddonInfo("path"))

    def _get_addon_profile_path(self):
        addon = self._get_addon()
        return xbmcvfs.translatePath(addon.getAddonInfo("profile"))

    def _get_log_path(self):
        log_path = xbmcvfs.translatePath("special://logpath/kodi.log")
        if xbmcvfs.exists(log_path):
            return log_path

        old_log_path = xbmcvfs.translatePath("special://logpath/kodi.old.log")
        if xbmcvfs.exists(old_log_path):
            return old_log_path

        return log_path

    def _get_upload_staging_dir(self):
        return xbmcvfs.translatePath("special://profile/addon_data/service.kodi_mcp/uploads")

    def _get_screenshot_dir(self):
        return xbmcvfs.translatePath("special://profile/addon_data/service.kodi_mcp/screenshots")

    def _read_binary_file(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def _capture_screenshot(self, include_image=False):
        screenshot_dir = self._get_screenshot_dir()
        if not xbmcvfs.exists(screenshot_dir):
            xbmcvfs.mkdirs(screenshot_dir)

        filename = "screenshot-%s.png" % int(time.time() * 1000)
        path = os.path.join(screenshot_dir, filename)
        xbmc.executebuiltin("TakeScreenshot(%s,true)" % path, wait=True)

        deadline = time.time() + 3
        content = b""
        while time.time() < deadline:
            if xbmcvfs.exists(path):
                content = self._read_binary_file(path)
                if len(content or b"") > 0:
                    break
            time.sleep(0.2)

        if not xbmcvfs.exists(path):
            return {
                "ok": False,
                "path": path,
                "error": "screenshot file not observed after request",
            }, 202

        if len(content or b"") == 0:
            return {
                "ok": False,
                "path": path,
                "error": "screenshot file was created but remained empty",
            }, 202

        result = {
            "ok": True,
            "path": path,
            "filename": os.path.basename(path),
            "size_bytes": len(content or b""),
            "content_type": "image/png",
        }
        if include_image:
            result["image_base64"] = base64.b64encode(content or b"").decode("ascii")
        return result, 200

    def _get_runtime_info(self):
        addon = self._get_addon()
        return {
            "addon_id": addon.getAddonInfo("id"),
            "addon_version": addon.getAddonInfo("version"),
            "addon_install_path": self._get_addon_install_path(),
            "addon_profile_path": self._get_addon_profile_path(),
            "kodi_log_path": self._get_log_path(),
            "bind_host": BRIDGE_BIND_HOST,
            "bind_port": BRIDGE_BIND_PORT,
        }

    def _get_addon_info(self, addon_id):
        addon_id = str(addon_id or "").strip()
        if not addon_id:
            return {"error": "addonid is required"}, 400

        installed = xbmc.getCondVisibility('System.HasAddon(%s)' % addon_id)
        enabled = xbmc.getCondVisibility('System.AddonIsEnabled(%s)' % addon_id)

        if not installed:
            return {
                "addon_id": addon_id,
                "installed": False,
                "enabled": False,
                "version": None,
                "install_path": None,
                "profile_path": None,
            }, 200

        try:
            addon = xbmcaddon.Addon(id=addon_id)
        except Exception as exc:
            return {
                "addon_id": addon_id,
                "installed": True,
                "enabled": enabled,
                "version": None,
                "install_path": None,
                "profile_path": None,
                "error": "failed to inspect addon: %s" % exc,
            }, 200

        return {
            "addon_id": addon_id,
            "installed": True,
            "enabled": enabled,
            "version": addon.getAddonInfo("version"),
            "install_path": xbmcvfs.translatePath(addon.getAddonInfo("path")),
            "profile_path": xbmcvfs.translatePath(addon.getAddonInfo("profile")),
        }, 200

    def _ensure_addon_enabled(self, addon_id):
        result, status = self._get_addon_info(addon_id)
        if status != 200:
            return result, status

        if not result.get("installed"):
            result["changed"] = False
            return result, 200

        if result.get("enabled"):
            result["changed"] = False
            return result, 200

        xbmc.executebuiltin('EnableAddon(%s)' % addon_id, wait=True)
        time.sleep(1)
        refreshed_result, refreshed_status = self._get_addon_info(addon_id)
        if refreshed_status != 200:
            return refreshed_result, refreshed_status
        refreshed_result["changed"] = True
        return refreshed_result, 200

    def _execute_addon(self, addon_id):
        result, status = self._get_addon_info(addon_id)
        if status != 200:
            return result, status

        if not result.get("installed"):
            result["executed"] = False
            return result, 200

        xbmc.executebuiltin('RunAddon(%s)' % addon_id, wait=False)
        result["executed"] = True
        return result, 200

    def _execute_builtin(self, command, addon_id=None):
        command = str(command or "").strip()
        addon_id = str(addon_id or "").strip()
        allowed = {"UpdateAddonRepos", "InstallAddon"}
        if not command:
            return {"error": "command is required", "allowed": sorted(allowed)}, 400
        if command not in allowed:
            return {"error": "command not allowed", "command": command, "allowed": sorted(allowed)}, 403

        if command == "InstallAddon":
            if not addon_id:
                return {"error": "addonid is required for InstallAddon", "allowed": sorted(allowed)}, 400
            builtin = 'InstallAddon(%s)' % addon_id
        else:
            builtin = command

        xbmc.executebuiltin(builtin, wait=False)
        return {"ok": True, "executed": command, "builtin": builtin, "addon_id": addon_id or None}, 200

    def _check_addon_version(self, addon_id, expected_version):
        result, status = self._get_addon_info(addon_id)
        if status != 200:
            return result, status

        expected_version = str(expected_version or "").strip()
        if not result.get("installed"):
            return {
                "addon_id": result.get("addon_id"),
                "installed": False,
                "actual_version": None,
                "expected_version": expected_version,
                "matches": False,
            }, 200

        actual_version = result.get("version")
        return {
            "addon_id": result.get("addon_id"),
            "installed": True,
            "actual_version": actual_version,
            "expected_version": expected_version,
            "matches": actual_version == expected_version,
        }, 200

    def _upload_addon_zip(self, filename, body):
        filename = os.path.basename(str(filename or "").strip())
        if not filename:
            return {"error": "filename is required"}, 400
        if not filename.lower().endswith('.zip'):
            return {"error": "filename must end with .zip"}, 400
        if body is None:
            return {"error": "request body is required"}, 400
        if len(body) > MAX_UPLOAD_BYTES:
            return {"error": "upload exceeds max size", "max_bytes": MAX_UPLOAD_BYTES}, 413

        staging_dir = self._get_upload_staging_dir()
        if not xbmcvfs.exists(staging_dir):
            xbmcvfs.mkdirs(staging_dir)

        saved_path = os.path.join(staging_dir, filename)
        handle = xbmcvfs.File(saved_path, 'wb')
        try:
            handle.write(body)
        finally:
            handle.close()

        return {
            "filename": filename,
            "saved_path": saved_path,
            "size_bytes": len(body),
            "ok": True,
        }, 200

    def _is_allowed_path(self, requested_path):
        normalized = os.path.normcase(os.path.abspath(requested_path))
        allowed_prefixes = [
            os.path.normcase(os.path.abspath(self._get_addon_install_path())),
            os.path.normcase(os.path.abspath(self._get_addon_profile_path())),
        ]
        kodi_log_path = os.path.normcase(os.path.abspath(self._get_log_path()))

        if normalized == kodi_log_path:
            return True

        for prefix in allowed_prefixes:
            if normalized.startswith(prefix):
                return True

        return False

    def _read_text_file(self, requested_path):
        if not requested_path:
            return {"error": "path is required"}, 400

        translated_path = xbmcvfs.translatePath(requested_path)
        normalized = os.path.abspath(translated_path)
        if not self._is_allowed_path(normalized):
            return {"error": "path not allowed", "path": normalized}, 403

        if not xbmcvfs.exists(normalized):
            return {"error": "file not found", "path": normalized}, 404

        handle = xbmcvfs.File(normalized)
        try:
            content = handle.read(MAX_FILE_READ_BYTES)
        finally:
            handle.close()

        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")

        return {
            "path": normalized,
            "content": content,
            "truncated": len(content.encode("utf-8")) >= MAX_FILE_READ_BYTES,
            "max_bytes": MAX_FILE_READ_BYTES,
        }, 200

    def _inspect_addon_db(self, addon_id):
        if not addon_id:
            return {"error": "addonid is required"}, 400
        try:
            import glob
            import sqlite3
            db_glob = xbmcvfs.translatePath("special://profile/Database/Addons*.db")
            candidates = sorted(glob.glob(db_glob))
            if not candidates:
                return {"error": "no addons db found", "glob": db_glob}, 404
            db_path = candidates[-1]
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                out = {"db_path": db_path, "addonid": addon_id}
                cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                out["tables"] = [r[0] for r in cur.fetchall()]
                if "addons" in out["tables"]:
                    cur = conn.execute("SELECT * FROM addons WHERE addonID = ?", (addon_id,))
                    out["addons_rows"] = [dict(r) for r in cur.fetchall()]
                if "repo" in out["tables"]:
                    cur = conn.execute("SELECT * FROM repo WHERE addonID = ?", (addon_id,))
                    out["repo_rows"] = [dict(r) for r in cur.fetchall()]
                if "addonlinkrepo" in out["tables"] and "addons" in out["tables"] and "repo" in out["tables"]:
                    cur = conn.execute(
                        "SELECT a.addonID AS addonID, a.version AS version, r.addonID AS repoID, r.checksum AS checksum, r.datadir AS datadir, r.info AS info "
                        "FROM addons a JOIN addonlinkrepo alr ON a.id = alr.idAddon JOIN repo r ON r.id = alr.idRepo "
                        "WHERE a.addonID = ? OR r.addonID = ?",
                        (addon_id, addon_id),
                    )
                    out["linked_rows"] = [dict(r) for r in cur.fetchall()]
                return out, 200
            finally:
                conn.close()
        except Exception as exc:
            return {"error": str(exc)}, 500

    def _read_log_tail(self, lines=50):
        addon = self._get_addon()
        log_path = self._get_log_path()
        if not xbmcvfs.exists(log_path):
            return {
                "lines": [],
                "path": log_path,
                "error": "log file not found",
            }

        handle = xbmcvfs.File(log_path)
        try:
            content = handle.read()
        finally:
            handle.close()

        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")

        tail_lines = content.splitlines()[-lines:]
        return {
            "lines": tail_lines,
            "path": log_path,
            "error": None,
            "addon_id": addon.getAddonInfo("id"),
        }

    def _read_log_markers(self, lines=100):
        result = self._read_log_tail(lines=lines)
        markers = [
            line
            for line in result.get("lines", [])
            if "[service.kodi_mcp]" in line
            or "[service.kodi_mcp][MARKER]" in line
            or "[service.kodi_mcp][DEBUG_PING]" in line
        ]
        result["lines"] = markers
        return result

    def do_GET(self):
        parsed = urlparse(self.path)

        if not self._is_public_get_path(parsed.path) and not self._require_token_auth():
            return

        if parsed.path == "/health":
            addon = xbmcaddon.Addon()
            self._write_json(
                {
                    "status": "ok",
                    "service": "service.kodi_mcp",
                    "addon_id": addon.getAddonInfo("id"),
                    "version": addon.getAddonInfo("version"),
                }
            )
            return

        if parsed.path == "/ping":
            addon = xbmcaddon.Addon()
            timestamp = int(time.time())
            self._write_json(
                {
                    "addon_id": addon.getAddonInfo("id"),
                    "addon_version": addon.getAddonInfo("version"),
                    "timestamp": timestamp,
                }
            )
            return

        if parsed.path == "/version":
            addon = xbmcaddon.Addon()
            self._write_json(
                {
                    "addon_id": addon.getAddonInfo("id"),
                    "version": addon.getAddonInfo("version"),
                }
            )
            return

        if parsed.path in ("/capabilities", "/control/capabilities"):
            addon = xbmcaddon.Addon()
            self._write_json(
                {
                    "addon_id": addon.getAddonInfo("id"),
                    "control_api_version": 1,
                    "endpoints": {
                        "health": {
                            "method": "GET",
                            "path": "/health",
                        },
                        "ping": {
                            "method": "GET",
                            "path": "/ping",
                        },
                        "version": {
                            "method": "GET",
                            "path": "/version",
                        },
                        "debug_ping": {
                            "method": "POST",
                            "path": "/debug/ping",
                        },
                        "mcp_register": {
                            "method": "POST",
                            "path": "/mcp/register",
                            "auth_required": True,
                            "auth_header": AUTH_HEADER_TOKEN,
                        },
                        "mcp_state": {
                            "method": "GET",
                            "path": "/mcp/state",
                            "auth_required": True,
                            "auth_header": AUTH_HEADER_TOKEN,
                        },
                        "repo_stage": {
                            "method": "POST",
                            "path": "/repo/stage",
                            "auth_required": True,
                            "auth_header": AUTH_HEADER_TOKEN,
                        },
                        "gui_action": {
                            "method": "POST",
                            "path": "/gui/action",
                            "auth_required": True,
                            "auth_header": AUTH_HEADER_TOKEN,
                        },
                        "gui_screenshot": {
                            "method": "GET",
                            "path": "/gui/screenshot",
                            "auth_required": True,
                            "auth_header": AUTH_HEADER_TOKEN,
                        },
                    },
                    "features": {
                        "liveness_probe": True,
                        "version_probe": True,
                        "debug_ping": True,
                        "lifecycle_control": False,
                        "mcp_registration": True,
                        "repo_zip_staging": True,
                        "gui_actions": sorted(GUI_ACTIONS.keys()),
                        "screenshots": True,
                    },
                }
            )
            return

        if parsed.path == "/mcp/state":
            if not self._require_token_auth():
                return

            state = load_state()
            derived = self._compute_derived_state(state)
            self._write_envelope(
                {
                    "ok": True,
                    "schema_version": int(state.get("schema_version") or STATE_SCHEMA_VERSION),
                    "state_rev": int(state.get("state_rev") or 0),
                    "registration": state.get("registration"),
                    "repo_zip": state.get("repo_zip"),
                    "derived": {
                        "now": derived.get("now"),
                        "registration_present": derived.get("registration_present"),
                        "registration_age_seconds": derived.get("registration_age_seconds"),
                        "registration_stale": derived.get("registration_stale"),
                        "repo_zip_present_in_state": derived.get("repo_zip_present_in_state"),
                        "repo_zip_file_exists": derived.get("repo_zip_file_exists"),
                        "dev_setup_available": derived.get("dev_setup_available"),
                    },
                },
                status=200,
            )
            return

        if parsed.path == "/status":
            addon = self._get_addon()
            self._write_json(
                {
                    "addon_id": addon.getAddonInfo("id"),
                    "addon_version": addon.getAddonInfo("version"),
                    "bind_host": BRIDGE_BIND_HOST,
                    "bind_port": BRIDGE_BIND_PORT,
                    "log_path": self._get_log_path(),
                    "uptime_seconds": int(time.time() - BRIDGE_START_TIME),
                }
            )
            return

        if parsed.path == "/runtime/info":
            self._write_json(self._get_runtime_info())
            return

        if parsed.path == "/gui/screenshot":
            query = parse_qs(parsed.query)
            include_image = str(query.get("include_image", ["false"])[0]).lower() in ("1", "true", "yes")
            result, status = self._capture_screenshot(include_image=include_image)
            self._write_json(result, status=status)
            return

        if parsed.path == "/files/read":
            query = parse_qs(parsed.query)
            requested_path = query.get("path", [""])[0]
            result, status = self._read_text_file(requested_path)
            self._write_json(result, status=status)
            return

        if parsed.path == "/debug/addon-db":
            query = parse_qs(parsed.query)
            addon_id = query.get("addonid", [""])[0]
            result, status = self._inspect_addon_db(addon_id)
            self._write_json(result, status=status)
            return

        if parsed.path == "/addon/info":
            query = parse_qs(parsed.query)
            addon_id = query.get("addonid", [""])[0]
            result, status = self._get_addon_info(addon_id)
            self._write_json(result, status=status)
            return

        if parsed.path == "/log/tail":
            query = parse_qs(parsed.query)
            try:
                lines = int(query.get("lines", ["50"])[0])
            except ValueError:
                self._write_json(
                    {
                        "error": "invalid lines parameter",
                    },
                    status=400,
                )
                return

            if lines < 1:
                self._write_json(
                    {
                        "error": "lines must be >= 1",
                    },
                    status=400,
                )
                return

            result = self._read_log_tail(lines=lines)
            self._write_json(result)
            return

        if parsed.path == "/log/markers":
            query = parse_qs(parsed.query)
            try:
                lines = int(query.get("lines", ["100"])[0])
            except ValueError:
                self._write_json(
                    {
                        "error": "invalid lines parameter",
                    },
                    status=400,
                )
                return

            if lines < 1:
                self._write_json(
                    {
                        "error": "lines must be >= 1",
                    },
                    status=400,
                )
                return

            result = self._read_log_markers(lines=lines)
            self._write_json(result)
            return

        if parsed.path == "/addon/version-check":
            query = parse_qs(parsed.query)
            addon_id = query.get("addonid", [""])[0]
            expected_version = query.get("expected_version", [""])[0]
            result, status = self._check_addon_version(addon_id, expected_version)
            self._write_json(result, status=status)
            return

        self._write_json({"error": "not found"}, status=404)

    def do_POST(self):
        parsed = urlparse(self.path)

        if not self._require_token_auth():
            return

        if parsed.path == "/gui/action":
            payload, error = self._read_json_body()
            if error:
                self._write_json({"error": error}, status=400)
                return

            result, status = self._gui_action((payload or {}).get("action"))
            self._write_json(result, status=status)
            return

        if parsed.path == "/log/marker":
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self._write_json({"error": "invalid json body"}, status=400)
                return

            message = str(payload.get("message", "")).strip()
            if not message:
                self._write_json({"error": "message is required"}, status=400)
                return

            marker = "[service.kodi_mcp][MARKER] %s" % message
            xbmc.log(marker, xbmc.LOGINFO)
            self._write_json(
                {
                    "status": "ok",
                    "marker": marker,
                }
            )
            return

        if parsed.path == "/debug/ping":
            addon = xbmcaddon.Addon()
            timestamp = int(time.time())
            marker = "[service.kodi_mcp][DEBUG_PING] addon_id=%s addon_version=%s timestamp=%s" % (
                addon.getAddonInfo("id"),
                addon.getAddonInfo("version"),
                timestamp,
            )
            xbmc.log(marker, xbmc.LOGINFO)
            self._write_json(
                {
                    "addon_id": addon.getAddonInfo("id"),
                    "addon_version": addon.getAddonInfo("version"),
                    "timestamp": timestamp,
                }
            )
            return

        if parsed.path == "/addon/ensure-enabled":
            query = parse_qs(parsed.query)
            addon_id = query.get("addonid", [""])[0]
            result, status = self._ensure_addon_enabled(addon_id)
            self._write_json(result, status=status)
            return

        if parsed.path == "/addon/execute":
            query = parse_qs(parsed.query)
            addon_id = query.get("addonid", [""])[0]
            result, status = self._execute_addon(addon_id)
            self._write_json(result, status=status)
            return

        if parsed.path == "/execute_builtin":
            query = parse_qs(parsed.query)
            command = query.get("command", [""])[0]
            addon_id = query.get("addonid", [""])[0]
            result, status = self._execute_builtin(command, addon_id=addon_id)
            self._write_json(result, status=status)
            return

        if parsed.path == "/addon/upload":
            query = parse_qs(parsed.query)
            filename = query.get("filename", [""])[0]
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length) if content_length > 0 else b""
            result, status = self._upload_addon_zip(filename, body)
            self._write_json(result, status=status)
            return

        if parsed.path == "/mcp/register":
            if not self._require_token_auth():
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self._write_envelope(
                    {"ok": False, "error_code": "INVALID_JSON", "message": "invalid json body"},
                    status=400,
                )
                return

            if not isinstance(payload, dict):
                self._write_envelope(
                    {"ok": False, "error_code": "INVALID_JSON", "message": "json body must be an object"},
                    status=400,
                )
                return

            required = [
                "control_api_version",
                "server_id",
                "server_instance_id",
                "server_base_url",
                "ttl_seconds",
            ]
            missing = []
            for k in required:
                if k in ("control_api_version", "ttl_seconds"):
                    if payload.get(k) is None:
                        missing.append(k)
                else:
                    if not str(payload.get(k) or "").strip():
                        missing.append(k)
            if missing:
                self._write_envelope(
                    {
                        "ok": False,
                        "error_code": "MISSING_FIELDS",
                        "message": "missing required fields",
                        "missing": missing,
                    },
                    status=400,
                )
                return

            try:
                control_api_version = int(payload.get("control_api_version"))
            except Exception:
                self._write_envelope(
                    {"ok": False, "error_code": "INVALID_FIELD", "field": "control_api_version"},
                    status=400,
                )
                return

            if control_api_version != 1:
                self._write_envelope(
                    {
                        "ok": False,
                        "error_code": "UNSUPPORTED_CONTROL_API_VERSION",
                        "supported": [1],
                    },
                    status=200,
                )
                return

            try:
                requested_ttl = int(payload.get("ttl_seconds"))
            except Exception:
                self._write_envelope(
                    {"ok": False, "error_code": "INVALID_FIELD", "field": "ttl_seconds"},
                    status=400,
                )
                return

            applied_ttl = max(10, min(3600, requested_ttl))
            now = _now_epoch_seconds()
            features_in = payload.get("features") or {}
            repo_zip_staging = False
            if isinstance(features_in, dict):
                repo_zip_staging = bool(features_in.get("repo_zip_staging"))

            started_at_in = payload.get("started_at")
            try:
                started_at = int(started_at_in) if started_at_in is not None else None
            except Exception:
                started_at = None

            registration = {
                "control_api_version": control_api_version,
                "server_id": str(payload.get("server_id") or "").strip(),
                "server_instance_id": str(payload.get("server_instance_id") or "").strip(),
                "server_base_url": str(payload.get("server_base_url") or "").strip(),
                "mcp_endpoint_url": str(payload.get("mcp_endpoint_url") or "").strip() or None,
                "server_version": str(payload.get("server_version") or "").strip() or None,
                "started_at": started_at,
                "registered_at": now,
                "last_seen_at": now,
                "requested_ttl_seconds": requested_ttl,
                "applied_ttl_seconds": applied_ttl,
                "features": {"repo_zip_staging": repo_zip_staging},
            }

            state = load_state()
            state["registration"] = registration
            state = save_state(state)

            expires_at = now + applied_ttl
            self._write_envelope(
                {
                    "ok": True,
                    "action": "registered_or_refreshed",
                    "state_rev": int(state.get("state_rev") or 0),
                    "stored_at": now,
                    "registration": registration,
                    "derived": {
                        "expires_at": expires_at,
                        "registration_stale": False,
                    },
                },
                status=200,
            )
            return

        if parsed.path == "/repo/stage":
            if not self._require_token_auth():
                return

            query = parse_qs(parsed.query)
            repo_id = str(query.get("repo_id", [""])[0] or "").strip()
            if not repo_id:
                self._write_envelope(
                    {"ok": False, "error_code": "MISSING_FIELD", "field": "repo_id"},
                    status=400,
                )
                return

            if not REPO_ID_RE.match(repo_id):
                self._write_envelope(
                    {
                        "ok": False,
                        "error_code": "INVALID_FIELD",
                        "field": "repo_id",
                        "message": "repo_id must match %s" % REPO_ID_RE.pattern,
                    },
                    status=400,
                )
                return

            mode = str(query.get("mode", ["overwrite"])[0] or "overwrite").strip().lower()
            if mode not in ("overwrite", "fail_if_exists"):
                self._write_envelope(
                    {
                        "ok": False,
                        "error_code": "INVALID_FIELD",
                        "field": "mode",
                        "allowed": ["overwrite", "fail_if_exists"],
                    },
                    status=400,
                )
                return

            content_type = str(self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "application/zip":
                self._write_envelope(
                    {
                        "ok": False,
                        "error_code": "INVALID_CONTENT_TYPE",
                        "expected": "application/zip",
                        "actual": content_type,
                    },
                    status=400,
                )
                return

            raw_len = str(self.headers.get("Content-Length") or "").strip()
            if not raw_len:
                self._write_envelope(
                    {"ok": False, "error_code": "MISSING_HEADER", "header": "Content-Length"},
                    status=400,
                )
                return

            try:
                content_length = int(raw_len)
            except Exception:
                self._write_envelope(
                    {"ok": False, "error_code": "INVALID_HEADER", "header": "Content-Length"},
                    status=400,
                )
                return

            if content_length < 0:
                self._write_envelope(
                    {"ok": False, "error_code": "INVALID_HEADER", "header": "Content-Length"},
                    status=400,
                )
                return

            if content_length > MAX_REPO_ZIP_UPLOAD_BYTES:
                self._write_envelope(
                    {
                        "ok": False,
                        "error_code": "PAYLOAD_TOO_LARGE",
                        "max_bytes": MAX_REPO_ZIP_UPLOAD_BYTES,
                        "size_bytes": content_length,
                    },
                    status=413,
                )
                return

            expected_sha256 = str(self.headers.get("X-Content-SHA256") or "").strip().lower() or None
            if expected_sha256 is not None:
                if not re.match(r"^[0-9a-f]{64}$", expected_sha256):
                    self._write_envelope(
                        {
                            "ok": False,
                            "error_code": "INVALID_HEADER",
                            "header": "X-Content-SHA256",
                        },
                        status=400,
                    )
                    return

            repo_version = str(self.headers.get("X-Repo-Version") or "").strip() or None

            # Prepare paths
            dir_translated = _translate(DEV_REPO_DIR_SPECIAL)
            if not xbmcvfs.exists(dir_translated):
                xbmcvfs.mkdirs(dir_translated)

            special_tmp = "%s/%s.zip.tmp" % (DEV_REPO_DIR_SPECIAL.rstrip("/"), repo_id)
            special_final = "%s/%s.zip" % (DEV_REPO_DIR_SPECIAL.rstrip("/"), repo_id)
            tmp_translated = _translate(special_tmp)
            final_translated = _translate(special_final)

            if mode == "fail_if_exists" and xbmcvfs.exists(final_translated):
                self._write_envelope(
                    {"ok": False, "error_code": "ALREADY_EXISTS", "message": "repo zip already staged"},
                    status=200,
                )
                return

            # Stream upload to tmp and compute SHA256
            sha = hashlib.sha256()
            remaining = content_length
            handle = xbmcvfs.File(tmp_translated, "wb")
            bytes_written = 0
            try:
                while remaining > 0:
                    to_read = UPLOAD_CHUNK_SIZE if remaining > UPLOAD_CHUNK_SIZE else remaining
                    chunk = self.rfile.read(to_read)
                    if not chunk:
                        break
                    sha.update(chunk)
                    handle.write(chunk)
                    bytes_written += len(chunk)
                    remaining -= len(chunk)
            finally:
                handle.close()

            if bytes_written != content_length:
                if xbmcvfs.exists(tmp_translated):
                    xbmcvfs.delete(tmp_translated)
                self._write_envelope(
                    {
                        "ok": False,
                        "error_code": "INCOMPLETE_UPLOAD",
                        "expected_bytes": content_length,
                        "received_bytes": bytes_written,
                    },
                    status=400,
                )
                return

            actual_sha256 = sha.hexdigest()
            if expected_sha256 and not hmac.compare_digest(expected_sha256, actual_sha256):
                if xbmcvfs.exists(tmp_translated):
                    xbmcvfs.delete(tmp_translated)
                self._write_envelope(
                    {
                        "ok": False,
                        "error_code": "CHECKSUM_MISMATCH",
                        "expected_sha256": expected_sha256,
                        "actual_sha256": actual_sha256,
                    },
                    status=200,
                )
                return

            # Activate: overwrite active staged zip directly
            if xbmcvfs.exists(final_translated):
                xbmcvfs.delete(final_translated)
            if not xbmcvfs.rename(tmp_translated, final_translated):
                # best-effort cleanup
                if xbmcvfs.exists(tmp_translated):
                    xbmcvfs.delete(tmp_translated)
                self._write_envelope(
                    {"ok": False, "error_code": "STAGE_FAILED", "message": "failed to activate staged zip"},
                    status=500,
                )
                return

            now = _now_epoch_seconds()
            repo_zip = {
                "repo_id": repo_id,
                "repo_version": repo_version,
                "special_path": special_final,
                "size_bytes": content_length,
                "sha256": actual_sha256,
                "staged_at": now,
            }

            state = load_state()
            state["repo_zip"] = repo_zip
            state = save_state(state)

            self._write_envelope(
                {
                    "ok": True,
                    "state_rev": int(state.get("state_rev") or 0),
                    "repo_zip": {
                        "repo_id": repo_zip.get("repo_id"),
                        "repo_version": repo_zip.get("repo_version"),
                        "special_path": repo_zip.get("special_path"),
                        "translated_path": final_translated,
                        "size_bytes": repo_zip.get("size_bytes"),
                        "sha256": repo_zip.get("sha256"),
                        "staged_at": repo_zip.get("staged_at"),
                    },
                    "derived": {
                        "repo_zip_file_exists": xbmcvfs.exists(final_translated),
                    },
                },
                status=200,
            )
            return

        if parsed.path == "/repo/refresh":
            xbmc.log("[service.kodi_mcp] Repository refresh requested", xbmc.LOGINFO)
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                json.loads(body.decode("utf-8"))
            except Exception:
                self._write_json({"error": "invalid json body"}, status=400)
                return
            try:
                xbmc.executebuiltin('UpdateAddonRepos', wait=True)
                self._write_json({"ok": True, "message": "repository refresh initiated"})
            except Exception:
                self._write_json({"error": "repository refresh failed"}, status=500)
            return

        self._write_json({"error": "not found"}, status=404)


class KodiBridgeServer(object):
    """Threaded local HTTP server wrapper."""

    def __init__(self, host=BRIDGE_BIND_HOST, port=BRIDGE_BIND_PORT):
        self.host = host
        self.port = port
        self.httpd = None
        self.thread = None

    def start(self):
        self.httpd = ThreadingHTTPServer((self.host, self.port), KodiBridgeHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        xbmc.log(
            "[service.kodi_mcp] HTTP bridge listening on %s:%s" % (self.host, self.port),
            xbmc.LOGINFO,
        )

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=2)
            self.thread = None
        xbmc.log("[service.kodi_mcp] HTTP bridge stopped", xbmc.LOGINFO)
