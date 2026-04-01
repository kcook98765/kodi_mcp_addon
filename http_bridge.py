# -*- coding: utf-8 -*-
"""Minimal local HTTP bridge for Kodi MCP development."""

import json
import os
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
