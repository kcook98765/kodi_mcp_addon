# -*- coding: utf-8 -*-
"""Minimal local HTTP bridge for Kodi MCP development."""

import json
import hashlib
import base64
import os
import threading
import time
import uuid
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
MAX_REPO_STAGE_BYTES = 25 * 1024 * 1024
DEFAULT_REPO_ID = "dev-repo"
DEFAULT_REPOSITORY_ADDON_ID = "repository.kodi-mcp"
DEFAULT_REPOSITORY_NAME = "Kodi MCP Repository"
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
MCP_STATE_LOCK = threading.Lock()
MCP_STATE = {
    "registration": None,
    "repo_zip": None,
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

    def _get_addon(self):
        return xbmcaddon.Addon()

    def _get_shared_token(self):
        try:
            return str(self._get_addon().getSetting("mcp_token") or "").strip()
        except Exception:
            return ""

    def _request_token(self):
        return str(self.headers.get("X-Kodi-MCP-Token", "") or "").strip()

    def _authorize(self):
        expected = self._get_shared_token()
        if not expected:
            return True
        return self._request_token() == expected

    def _write_auth_error_if_needed(self):
        if self._authorize():
            return False
        self._write_json({"error": "unauthorized"}, status=401)
        return True

    def _read_json_body(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            return json.loads(body.decode("utf-8")), None
        except Exception:
            return None, "invalid json body"

    def _standard_envelope(self, result=None, ok=True, error=None):
        result_payload = {
            "ok": bool(ok),
            "error": error,
        }
        if isinstance(result, dict):
            result_payload.update(result)
        else:
            result_payload["data"] = result

        return {
            "transport": {
                "ok": True,
                "bridge": "service.kodi_mcp",
                "request_id": str(uuid.uuid4()),
            },
            "result": result_payload,
        }

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

    def _get_repo_staging_dir(self):
        return xbmcvfs.translatePath("special://profile/addon_data/service.kodi_mcp/repo_stage")

    def _get_repo_stage_path(self, repo_id):
        repo_id = str(repo_id or "").strip() or DEFAULT_REPO_ID
        return os.path.join(self._get_repo_staging_dir(), "%s.zip" % repo_id)

    def _get_screenshot_dir(self):
        return xbmcvfs.translatePath("special://profile/addon_data/service.kodi_mcp/screenshots")

    def _get_capabilities(self):
        return {
            "service": "service.kodi_mcp",
            "bridge_api_version": 1,
            "endpoints": [
                "/health",
                "/status",
                "/runtime/info",
                "/capabilities",
                "/control/capabilities",
                "/mcp/register",
                "/mcp/state",
                "/repo/stage",
                "/gui/action",
                "/gui/screenshot",
                "/addon/info",
                "/addon/ensure-enabled",
                "/addon/execute",
                "/addon/version-check",
                "/log/tail",
                "/log/markers",
                "/log/marker",
                "/files/read",
                "/debug/addon-db",
                "/debug/ping",
            ],
            "features": {
                "token_auth": True,
                "mcp_registration": True,
                "repo_zip_staging": True,
                "repo_zip_sha256": True,
                "repo_zip_state_rehydrate": True,
                "gui_actions": sorted(GUI_ACTIONS.keys()),
                "screenshots": True,
            },
            "limits": {
                "max_file_read_bytes": MAX_FILE_READ_BYTES,
                "max_upload_bytes": MAX_UPLOAD_BYTES,
                "max_repo_stage_bytes": MAX_REPO_STAGE_BYTES,
            },
        }

    def _jsonrpc(self, method, params=None):
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
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

    def _capture_screenshot(self, include_image=False):
        screenshot_dir = self._get_screenshot_dir()
        if not xbmcvfs.exists(screenshot_dir):
            xbmcvfs.mkdirs(screenshot_dir)

        filename = "screenshot-%s.png" % int(time.time() * 1000)
        path = os.path.join(screenshot_dir, filename)
        xbmc.executebuiltin("TakeScreenshot(%s,true)" % path, wait=True)

        # Kodi writes screenshots asynchronously; poll briefly for non-empty output.
        deadline = time.time() + 3
        candidate = path
        content = b""
        while time.time() < deadline:
            if xbmcvfs.exists(candidate):
                content = self._read_binary_file(candidate)
                if len(content or b"") > 0:
                    break
            time.sleep(0.2)

        if not xbmcvfs.exists(candidate):
            return {
                "ok": False,
                "path": candidate,
                "error": "screenshot file not observed after request",
            }, 202

        if len(content or b"") == 0:
            return {
                "ok": False,
                "path": candidate,
                "error": "screenshot file was created but remained empty",
            }, 202

        result = {
            "ok": True,
            "path": candidate,
            "filename": os.path.basename(candidate),
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

    def _write_binary_file(self, path, body):
        parent = os.path.dirname(path)
        if parent and not xbmcvfs.exists(parent):
            xbmcvfs.mkdirs(parent)
        with open(path, "wb") as handle:
            handle.write(body)

    def _read_binary_file(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def _repo_stage(self, repo_id, mode, body):
        repo_id = str(repo_id or "").strip() or DEFAULT_REPO_ID
        mode = str(mode or "").strip() or "overwrite"
        if mode != "overwrite":
            return self._standard_envelope(ok=False, error="only overwrite mode is supported"), 400
        if body is None or len(body) == 0:
            return self._standard_envelope(ok=False, error="request body is required"), 400
        if len(body) > MAX_REPO_STAGE_BYTES:
            return self._standard_envelope(ok=False, error="repo zip exceeds max size"), 413
        if not body.startswith(b"PK"):
            return self._standard_envelope(ok=False, error="repo stage body must be a zip file"), 400

        expected_sha = str(self.headers.get("X-Content-SHA256", "") or "").strip().lower()
        actual_sha = hashlib.sha256(body).hexdigest()
        if expected_sha and expected_sha != actual_sha:
            return self._standard_envelope(ok=False, error="sha256 mismatch"), 400

        filename = "%s.zip" % repo_id
        saved_path = self._get_repo_stage_path(repo_id)
        self._write_binary_file(saved_path, body)

        repo_zip = {
            "repo_id": repo_id,
            "mode": mode,
            "repo_version": str(self.headers.get("X-Repo-Version", "") or "").strip() or None,
            "filename": filename,
            "saved_path": saved_path,
            "size_bytes": len(body),
            "sha256": actual_sha,
            "staged_at": int(time.time()),
        }
        with MCP_STATE_LOCK:
            MCP_STATE["repo_zip"] = repo_zip

        return self._standard_envelope(result=repo_zip), 200

    def _load_repo_zip_state(self):
        """Rehydrate staged repo zip metadata after service restart when possible."""
        with MCP_STATE_LOCK:
            existing = MCP_STATE.get("repo_zip")
            if isinstance(existing, dict) and existing.get("saved_path") and xbmcvfs.exists(existing.get("saved_path")):
                return existing

        saved_path = self._get_repo_stage_path(DEFAULT_REPO_ID)
        if not xbmcvfs.exists(saved_path):
            return None

        body = self._read_binary_file(saved_path)

        repo_zip = {
            "repo_id": DEFAULT_REPO_ID,
            "mode": "overwrite",
            "repo_version": None,
            "filename": "%s.zip" % DEFAULT_REPO_ID,
            "saved_path": saved_path,
            "size_bytes": len(body or b""),
            "sha256": hashlib.sha256(body or b"").hexdigest(),
            "staged_at": None,
            "rehydrated": True,
        }
        with MCP_STATE_LOCK:
            MCP_STATE["repo_zip"] = repo_zip
        return repo_zip

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
            try:
                if os.path.commonpath([normalized, prefix]) == prefix:
                    return True
            except ValueError:
                continue
            if normalized == prefix:
                return True

        return False

    def _register_mcp_server(self, payload):
        if not isinstance(payload, dict):
            return self._standard_envelope(ok=False, error="json object body is required"), 400

        now = int(time.time())
        try:
            ttl_seconds = int(payload.get("ttl_seconds") or 60)
        except Exception:
            ttl_seconds = 60
        ttl_seconds = max(10, min(ttl_seconds, 3600))

        registration = dict(payload)
        registration["received_at"] = now
        registration["applied_ttl_seconds"] = ttl_seconds
        registration["expires_at"] = now + ttl_seconds

        with MCP_STATE_LOCK:
            MCP_STATE["registration"] = registration

        return self._standard_envelope(result={"registration": registration}), 200

    def _mcp_state(self):
        now = int(time.time())
        with MCP_STATE_LOCK:
            registration = MCP_STATE.get("registration")
            repo_zip = MCP_STATE.get("repo_zip")
        if not isinstance(repo_zip, dict):
            repo_zip = self._load_repo_zip_state()

        registration_present = isinstance(registration, dict)
        registration_stale = True
        if registration_present:
            try:
                registration_stale = int(registration.get("expires_at") or 0) <= now
            except Exception:
                registration_stale = True

        repo_zip_file_exists = False
        if isinstance(repo_zip, dict) and repo_zip.get("saved_path"):
            repo_zip_file_exists = xbmcvfs.exists(repo_zip.get("saved_path"))

        state = {
            "registration": registration,
            "repo_zip": repo_zip,
        }
        install_hint = None
        if isinstance(repo_zip, dict) and repo_zip.get("saved_path"):
            install_hint = {
                "action": "Kodi UI: Add-ons > Install from repository",
                "repository_addon_id": DEFAULT_REPOSITORY_ADDON_ID,
                "repository_name": DEFAULT_REPOSITORY_NAME,
                "path": repo_zip.get("saved_path"),
                "staged_repo_archive_path": repo_zip.get("saved_path"),
                "note": (
                    "The staged dev-repo.zip is repository content for the bridge/server refresh loop, "
                    "not an installable Kodi add-on zip. If the repository add-on is missing, install "
                    "repository.kodi-mcp once, then use Install from repository for target add-ons."
                ),
            }
        derived = {
            "registration_present": registration_present,
            "registration_stale": registration_stale,
            "repo_zip_file_exists": repo_zip_file_exists,
            "dev_setup_available": bool(registration_present and not registration_stale and repo_zip_file_exists),
        }
        return self._standard_envelope(result={"state": state, "derived": derived, "install_hint": install_hint}), 200

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

        if parsed.path == "/mcp/state":
            if self._write_auth_error_if_needed():
                return
            result, status = self._mcp_state()
            self._write_json(result, status=status)
            return

        if parsed.path in ("/capabilities", "/control/capabilities"):
            self._write_json(self._get_capabilities())
            return

        if parsed.path == "/gui/screenshot":
            query = parse_qs(parsed.query)
            include_image = str(query.get("include_image", ["false"])[0]).lower() in ("1", "true", "yes")
            result, status = self._capture_screenshot(include_image=include_image)
            self._write_json(result, status=status)
            return

        if parsed.path == "/health":
            self._write_json(
                {
                    "status": "ok",
                    "service": "service.kodi_mcp",
                }
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

        if parsed.path == "/mcp/register":
            if self._write_auth_error_if_needed():
                return
            payload, error = self._read_json_body()
            if error:
                self._write_json(self._standard_envelope(ok=False, error=error), status=400)
                return
            result, status = self._register_mcp_server(payload)
            self._write_json(result, status=status)
            return

        if parsed.path == "/repo/stage":
            if self._write_auth_error_if_needed():
                return
            query = parse_qs(parsed.query)
            repo_id = query.get("repo_id", ["dev-repo"])[0]
            mode = query.get("mode", ["overwrite"])[0]
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length) if content_length > 0 else b""
            result, status = self._repo_stage(repo_id, mode, body)
            self._write_json(result, status=status)
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
            payload, error = self._read_json_body()
            if error:
                self._write_json({"error": error}, status=400)
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
