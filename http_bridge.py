# -*- coding: utf-8 -*-
"""Minimal local HTTP bridge for Kodi MCP development.

Milestone A additions:
- Shared-token authenticated MCP registration + state endpoints
- Repo zip staging (server -> addon local path)
- Minimal persisted state in addon_data/service.kodi_mcp/state.json
"""

import base64
import glob
import hashlib
import hmac
import json
import os
import re
import shutil

import subtitle_overlay
import sqlite3
import stat
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import xbmc
import xbmcaddon
import xbmcgui
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
REPOSITORY_BOOTSTRAP_ADDON_ID = "repository.kodi-mcp"
REPOSITORY_BOOTSTRAP_REPO_ID = "dev-repo"
REPOSITORY_BOOTSTRAP_SPECIAL_PATH = DEV_REPO_DIR_SPECIAL + "/dev-repo.zip"
REPOSITORY_BOOTSTRAP_DEST_SPECIAL = "special://home/addons/repository.kodi-mcp"
REPOSITORY_BOOTSTRAP_MEMBERS = frozenset(
    (
        "repository.kodi-mcp/addon.xml",
        "repository.kodi-mcp/service.py",
        "repository.kodi-mcp/addons.xml",
    )
)
REPOSITORY_READINESS_MAX_ADDON_XML_BYTES = 128 * 1024
REPOSITORY_READINESS_MAX_METADATA_BYTES = 4 * 1024 * 1024
REPOSITORY_READINESS_MAX_CHECKSUM_BYTES = 1024
REPOSITORY_READINESS_TIMEOUT_SECONDS = 5
REPOSITORY_PACKAGE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
REPOSITORY_PACKAGE_VERSION_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._+~-]{0,127}$")
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
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


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _semantic_version(value):
    value = str(value or "").strip()
    if not SEMVER_RE.match(value):
        raise ValueError("repository bootstrap version must be semantic x.y.z")
    return tuple(int(part) for part in value.split("."))


def validate_repository_bootstrap(state):
    """Validate the one bridge-owned repository bootstrap slot.

    This intentionally accepts no path, URL, addon id, destination, or command.
    The only installable object is a fixed-ID repository.kodi-mcp ZIP previously
    staged into the bridge-owned dev-repo slot by the authenticated server. The
    bridge binds the staged metadata version to addon.xml instead of maintaining
    a second canonical-version constant.
    """

    repo_zip = (state or {}).get("repo_zip")
    if not isinstance(repo_zip, dict):
        raise ValueError("canonical repository bootstrap is not staged")
    if repo_zip.get("repo_id") != REPOSITORY_BOOTSTRAP_REPO_ID:
        raise ValueError("staged artifact is not the canonical repository bootstrap slot")
    staged_version = str(repo_zip.get("repo_version") or "").strip()
    _semantic_version(staged_version)
    if repo_zip.get("special_path") != REPOSITORY_BOOTSTRAP_SPECIAL_PATH:
        raise ValueError("staged repository bootstrap path is not canonical")

    expected_sha256 = str(repo_zip.get("sha256") or "").lower()
    if not re.match(r"^[0-9a-f]{64}$", expected_sha256):
        raise ValueError("staged repository bootstrap SHA-256 is missing or invalid")

    translated = _translate(REPOSITORY_BOOTSTRAP_SPECIAL_PATH)
    if not xbmcvfs.exists(translated) or not os.path.isfile(translated):
        raise ValueError("canonical repository bootstrap artifact is missing")
    expected_size = repo_zip.get("size_bytes")
    if not isinstance(expected_size, int) or expected_size < 1:
        raise ValueError("staged repository bootstrap size is missing or invalid")
    if os.path.getsize(translated) != expected_size:
        raise ValueError("staged repository bootstrap size mismatch")

    actual_sha256 = _sha256_file(translated)
    if not hmac.compare_digest(expected_sha256, actual_sha256):
        raise ValueError("staged repository bootstrap SHA-256 mismatch")

    try:
        with zipfile.ZipFile(translated, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or set(names) != REPOSITORY_BOOTSTRAP_MEMBERS:
                raise ValueError("repository bootstrap ZIP layout is not canonical")
            for info in infos:
                mode = (info.external_attr >> 16) & 0xFFFF
                if info.is_dir() or stat.S_ISLNK(mode):
                    raise ValueError("repository bootstrap ZIP contains an unsafe member")
            addon_xml = archive.read("repository.kodi-mcp/addon.xml")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("repository bootstrap ZIP is invalid: %s" % exc)

    try:
        root = ElementTree.fromstring(addon_xml)
    except Exception as exc:
        raise ValueError("repository bootstrap addon.xml is invalid: %s" % exc)
    if root.tag != "addon" or root.get("id") != REPOSITORY_BOOTSTRAP_ADDON_ID:
        raise ValueError("repository bootstrap addon id is not canonical")
    if root.get("version") != staged_version:
        raise ValueError("repository bootstrap addon version does not match staged metadata")
    if not any(
        child.tag == "extension" and child.get("point") == "xbmc.addon.repository"
        for child in list(root)
    ):
        raise ValueError("repository bootstrap repository extension is missing")

    return {
        "addon_id": REPOSITORY_BOOTSTRAP_ADDON_ID,
        "version": staged_version,
        "sha256": actual_sha256,
        "size_bytes": expected_size,
        "translated_path": translated,
    }


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
        return path in ("/health", "/health/deep", "/status", "/runtime/info", "/capabilities", "/control/capabilities")

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

    def _get_build_identity(self):
        manifest_path = os.path.join(self._get_addon_install_path(), "build_manifest.json")
        if not xbmcvfs.exists(manifest_path):
            return None
        handle = xbmcvfs.File(manifest_path)
        try:
            raw = handle.read()
        finally:
            handle.close()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            manifest = json.loads(raw or "{}")
        except Exception:
            return None
        if not isinstance(manifest, dict):
            return None
        return {
            key: manifest.get(key)
            for key in ("source_git_sha", "source_fingerprint_sha256")
            if manifest.get(key)
        } or None

    def _get_shallow_health(self):
        addon = self._get_addon()
        return {
            "status": "ok",
            "service": "service.kodi_mcp",
            "addon_id": addon.getAddonInfo("id"),
            "version": addon.getAddonInfo("version"),
            "build": self._get_build_identity(),
            "health_type": "shallow",
            "uptime_seconds": int(time.time() - BRIDGE_START_TIME),
        }

    def _get_deep_health(self):
        addon = self._get_addon()
        response, error = self._jsonrpc("JSONRPC.Ping")
        jsonrpc_ok = (
            error is None
            and isinstance(response, dict)
            and response.get("result") == "pong"
        )
        payload = {
            "status": "ok" if jsonrpc_ok else "error",
            "service": "service.kodi_mcp",
            "addon_id": addon.getAddonInfo("id"),
            "version": addon.getAddonInfo("version"),
            "build": self._get_build_identity(),
            "health_type": "deep",
            "uptime_seconds": int(time.time() - BRIDGE_START_TIME),
            "kodi_jsonrpc_ok": jsonrpc_ok,
        }
        if not jsonrpc_ok:
            payload["kodi_jsonrpc_error"] = error or (
                response.get("error") if isinstance(response, dict) else "unexpected response"
            )
        return payload, 200 if jsonrpc_ok else 503

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

    def _safe_info_label(self, label):
        try:
            return xbmc.getInfoLabel(label) or ""
        except Exception:
            return ""

    def _safe_cond_visibility(self, condition):
        try:
            return bool(xbmc.getCondVisibility(condition))
        except Exception:
            return False

    def _safe_current_window_id(self):
        try:
            return xbmcgui.getCurrentWindowId()
        except Exception:
            return None

    def _safe_current_dialog_id(self):
        try:
            return xbmcgui.getCurrentWindowDialogId()
        except Exception:
            return None

    def _gui_state(self):
        active_response, active_error = self._jsonrpc("Player.GetActivePlayers")
        active_players = []
        if isinstance(active_response, dict):
            active_players = active_response.get("result") or []
            if not isinstance(active_players, list):
                active_players = []

        current_window = self._safe_info_label("System.CurrentWindow")
        current_control = self._safe_info_label("System.CurrentControl")
        conditions = {
            "fullscreen_video": self._safe_cond_visibility("Window.IsActive(fullscreenvideo)"),
            "player_has_media": self._safe_cond_visibility("Player.HasMedia"),
            "player_has_video": self._safe_cond_visibility("Player.HasVideo"),
            "player_playing": self._safe_cond_visibility("Player.Playing"),
            "player_paused": self._safe_cond_visibility("Player.Paused"),
        }

        result = {
            "ok": active_error is None,
            "current_window": current_window,
            "current_window_id": self._safe_current_window_id(),
            "current_dialog_id": self._safe_current_dialog_id(),
            "current_control": current_control,
            "conditions": conditions,
            "active_players": active_players,
            "jsonrpc": active_response,
            "error": active_error,
        }
        return result, 200 if active_error is None else 500

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
            "build": self._get_build_identity(),
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

    def _fetch_repository_url(self, url, max_bytes, first_byte_only=False):
        """Fetch one installed-repository URL with strict size and scheme bounds."""

        parsed = urlparse(str(url or "").strip())
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return {
                "reachable": False,
                "http_status": None,
                "error": "repository URL must be credential-free HTTP or HTTPS",
            }

        headers = {"User-Agent": "Kodi-MCP-Repository-Readiness/1"}
        if first_byte_only:
            headers["Range"] = "bytes=0-0"
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=REPOSITORY_READINESS_TIMEOUT_SECONDS) as response:
                final_url = response.geturl()
                final = urlparse(final_url)
                if (
                    final.scheme not in ("http", "https")
                    or not final.hostname
                    or final.username is not None
                    or final.password is not None
                ):
                    return {
                        "reachable": False,
                        "http_status": getattr(response, "status", None),
                        "error": "repository redirect target is not permitted",
                    }
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    return {
                        "reachable": False,
                        "http_status": getattr(response, "status", None),
                        "error": "repository response exceeds readiness size limit",
                    }
                return {
                    "reachable": True,
                    "http_status": getattr(response, "status", None),
                    "content_type": response.headers.get("Content-Type"),
                    "bytes_read": len(body),
                    "body": body,
                }
        except HTTPError as exc:
            return {
                "reachable": False,
                "http_status": exc.code,
                "error": "repository HTTP request failed",
            }
        except URLError:
            return {
                "reachable": False,
                "http_status": None,
                "error": "repository URL is unreachable from Kodi",
            }
        except Exception:
            return {
                "reachable": False,
                "http_status": None,
                "error": "repository readiness request failed",
            }

    def _repository_catalog_evidence(self):
        """Return best-effort, read-only evidence from Kodi's addon catalog DB."""

        refresh = {
            "observable": False,
            "state": "unknown",
            "reason": "Kodi exposes no stable read-only repository refresh-completion API",
        }
        ingestion = {
            "observable": False,
            "state": "unknown",
            "reason": "Kodi addon catalog database evidence is unavailable",
        }
        try:
            pattern = xbmcvfs.translatePath("special://profile/Database/Addons*.db")
            candidates = sorted(glob.glob(pattern))
            if not candidates:
                return refresh, ingestion
            connection = sqlite3.connect("file:%s?mode=ro" % candidates[-1], uri=True)
            connection.row_factory = sqlite3.Row
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "repo" not in tables:
                    return refresh, ingestion
                row = connection.execute(
                    "SELECT checksum, lastcheck, version, nextcheck "
                    "FROM repo WHERE addonID = ?",
                    (REPOSITORY_BOOTSTRAP_ADDON_ID,),
                ).fetchone()
                if row is None:
                    return refresh, ingestion
                refresh = {
                    "observable": True,
                    "state": "repository_record_observed",
                    "evidence_source": "kodi_addons_database_internal",
                    "last_check": row["lastcheck"],
                    "next_check": row["nextcheck"],
                    "repository_version": row["version"],
                    "checksum": row["checksum"],
                    "completion_proven": False,
                    "freshness_proven": False,
                }
                if {"addons", "addonlinkrepo"}.issubset(tables):
                    count = connection.execute(
                        "SELECT COUNT(*) FROM addons a "
                        "JOIN addonlinkrepo al ON a.id = al.idAddon "
                        "JOIN repo r ON r.id = al.idRepo WHERE r.addonID = ?",
                        (REPOSITORY_BOOTSTRAP_ADDON_ID,),
                    ).fetchone()[0]
                    ingestion = {
                        "observable": True,
                        "state": "entries_observed" if count else "no_entries_observed",
                        "evidence_source": "kodi_addons_database_internal",
                        "entry_count": int(count),
                        "freshness_proven": False,
                    }
            finally:
                connection.close()
        except Exception:
            return refresh, ingestion
        return refresh, ingestion

    def _repository_readiness(self):
        """Inspect and probe only the installed repository.kodi-mcp addon."""

        addon, status = self._get_addon_info(REPOSITORY_BOOTSTRAP_ADDON_ID)
        base = {
            "ok": True,
            "addon_id": REPOSITORY_BOOTSTRAP_ADDON_ID,
            "installed": bool(addon.get("installed")) if isinstance(addon, dict) else False,
            "enabled": bool(addon.get("enabled")) if isinstance(addon, dict) else False,
            "installed_version": addon.get("version") if isinstance(addon, dict) else None,
            "configured_identity": {"addon_id": None, "version": None},
            "urls": {"metadata": None, "checksum": None, "datadir": None},
            "metadata": {"reachable": False, "parseable": False, "addon_count": None},
            "checksum": {
                "reachable": False,
                "published_md5": None,
                "computed_md5": None,
                "match": False,
            },
            "package": {
                "observable": False,
                "reachable": False,
                "probe_addon_id": None,
                "probe_addon_version": None,
                "probe_url": None,
            },
            "catalog_refresh": {
                "observable": False,
                "state": "unknown",
                "reason": "Kodi exposes no stable read-only repository refresh-completion API",
            },
            "catalog_ingestion": {
                "observable": False,
                "state": "unknown",
                "reason": "Kodi exposes no stable read-only repository catalog-ingestion API",
            },
        }
        if status != 200 or not isinstance(addon, dict):
            base["ok"] = False
            base["error"] = "repository addon inspection failed"
            return base, 200
        if not addon.get("installed"):
            return base, 200

        addon_xml_path = os.path.join(str(addon.get("install_path") or ""), "addon.xml")
        try:
            if not xbmcvfs.exists(addon_xml_path):
                raise ValueError("installed repository addon.xml is missing")
            handle = xbmcvfs.File(addon_xml_path)
            try:
                raw = handle.read()
            finally:
                handle.close()
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            if not isinstance(raw, bytes) or len(raw) > REPOSITORY_READINESS_MAX_ADDON_XML_BYTES:
                raise ValueError("installed repository addon.xml is invalid or oversized")
            root = ElementTree.fromstring(raw)
            configured_id = root.get("id")
            configured_version = root.get("version")
            dirs = []
            for extension in list(root):
                if extension.tag != "extension" or extension.get("point") != "xbmc.addon.repository":
                    continue
                dirs.extend(child for child in list(extension) if child.tag == "dir")
            if configured_id != REPOSITORY_BOOTSTRAP_ADDON_ID or len(dirs) != 1:
                raise ValueError("installed repository identity or extension layout is invalid")
            directory = dirs[0]
            metadata_url = str(directory.findtext("info") or "").strip()
            checksum_url = str(directory.findtext("checksum") or "").strip()
            datadir_node = directory.find("datadir")
            datadir_url = str(datadir_node.text if datadir_node is not None else "").strip()
            if not metadata_url or not checksum_url or not datadir_url:
                raise ValueError("installed repository URLs are incomplete")
        except Exception as exc:
            base["ok"] = False
            base["error"] = str(exc)
            return base, 200

        base["configured_identity"] = {
            "addon_id": configured_id,
            "version": configured_version,
        }
        base["urls"] = {
            "metadata": metadata_url,
            "checksum": checksum_url,
            "datadir": datadir_url,
        }

        metadata_probe = self._fetch_repository_url(
            metadata_url, REPOSITORY_READINESS_MAX_METADATA_BYTES
        )
        metadata_body = metadata_probe.pop("body", None)
        base["metadata"].update(metadata_probe)
        metadata_root = None
        package_candidate = None
        if metadata_probe.get("reachable") and isinstance(metadata_body, bytes):
            try:
                metadata_root = ElementTree.fromstring(metadata_body)
                if metadata_root.tag != "addons":
                    raise ValueError("repository metadata root is not addons")
                candidates = []
                for child in list(metadata_root):
                    addon_id = str(child.get("id") or "")
                    addon_version = str(child.get("version") or "")
                    if (
                        child.tag == "addon"
                        and REPOSITORY_PACKAGE_ID_RE.match(addon_id)
                        and REPOSITORY_PACKAGE_VERSION_RE.match(addon_version)
                    ):
                        candidates.append((addon_id, addon_version))
                base["metadata"]["parseable"] = True
                base["metadata"]["addon_count"] = len(candidates)
                package_candidate = candidates[0] if candidates else None
            except Exception:
                base["metadata"]["parseable"] = False
                base["metadata"]["addon_count"] = None
                base["metadata"]["error"] = "repository metadata is not valid Kodi addons XML"

        checksum_probe = self._fetch_repository_url(
            checksum_url, REPOSITORY_READINESS_MAX_CHECKSUM_BYTES
        )
        checksum_body = checksum_probe.pop("body", None)
        base["checksum"].update(checksum_probe)
        if isinstance(metadata_body, bytes):
            base["checksum"]["computed_md5"] = hashlib.md5(metadata_body).hexdigest()
        if checksum_probe.get("reachable") and isinstance(checksum_body, bytes):
            match = re.search(rb"(?i)(?:^|\s)([0-9a-f]{32})(?:\s|$)", checksum_body.strip())
            if match:
                base["checksum"]["published_md5"] = match.group(1).decode("ascii").lower()
        base["checksum"]["match"] = bool(
            base["checksum"].get("published_md5")
            and base["checksum"].get("computed_md5")
            and hmac.compare_digest(
                base["checksum"]["published_md5"], base["checksum"]["computed_md5"]
            )
        )

        if package_candidate is not None:
            package_id, package_version = package_candidate
            package_filename = "%s-%s.zip" % (package_id, package_version)
            package_url = "%s/%s/%s" % (
                datadir_url.rstrip("/"),
                quote(package_id, safe=""),
                quote(package_filename, safe=""),
            )
            base["package"].update(
                {
                    "observable": True,
                    "probe_addon_id": package_id,
                    "probe_addon_version": package_version,
                    "probe_url": package_url,
                }
            )
            package_probe = self._fetch_repository_url(package_url, 1, first_byte_only=True)
            package_probe.pop("body", None)
            base["package"].update(package_probe)

        catalog_refresh, catalog_ingestion = self._repository_catalog_evidence()
        base["catalog_refresh"] = catalog_refresh
        base["catalog_ingestion"] = catalog_ingestion

        return base, 200

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

    def _install_repository_bootstrap(self):
        """Install only the canonical, already-staged repository.kodi-mcp ZIP."""

        try:
            artifact = validate_repository_bootstrap(load_state())
        except ValueError as exc:
            return {"ok": False, "error_code": "INVALID_BOOTSTRAP", "message": str(exc)}, 409

        before, before_status = self._get_addon_info(REPOSITORY_BOOTSTRAP_ADDON_ID)
        if before_status != 200:
            return {"ok": False, "error_code": "ADDON_INSPECTION_FAILED"}, 500
        artifact_version = artifact.get("version")
        action = "installed"
        if before.get("installed"):
            try:
                installed_version = str(before.get("version") or "")
                comparison = (_semantic_version(installed_version) > _semantic_version(artifact_version)) - (
                    _semantic_version(installed_version) < _semantic_version(artifact_version)
                )
            except ValueError as exc:
                return {
                    "ok": False,
                    "error_code": "INVALID_INSTALLED_VERSION",
                    "addon_id": REPOSITORY_BOOTSTRAP_ADDON_ID,
                    "canonical_version": artifact_version,
                    "installed_version": before.get("version"),
                    "message": str(exc),
                }, 409
            if comparison > 0:
                return {
                    "ok": False,
                    "error_code": "DOWNGRADE_FORBIDDEN",
                    "addon_id": REPOSITORY_BOOTSTRAP_ADDON_ID,
                    "canonical_version": artifact_version,
                    "installed_version": installed_version,
                }, 409
            if comparison < 0:
                action = "upgraded"
            else:
                action = "already_installed"

        if action == "already_installed":
            if not before.get("enabled"):
                response, error = self._jsonrpc(
                    "Addons.SetAddonEnabled",
                    {"addonid": REPOSITORY_BOOTSTRAP_ADDON_ID, "enabled": True},
                )
                if error or (isinstance(response, dict) and response.get("error")):
                    return {"ok": False, "error_code": "ENABLE_FAILED"}, 500
            xbmc.executebuiltin("UpdateAddonRepos", wait=False)
            after, _ = self._get_addon_info(REPOSITORY_BOOTSTRAP_ADDON_ID)
            if after.get("version") != artifact_version or not after.get("enabled"):
                return {"ok": False, "error_code": "POST_INSTALL_VERIFICATION_FAILED"}, 500
            return {
                "ok": True,
                "action": "already_installed",
                "addon_id": REPOSITORY_BOOTSTRAP_ADDON_ID,
                "version": artifact_version,
                "enabled": bool(after.get("enabled")),
                "artifact_sha256": artifact.get("sha256"),
            }, 200

        destination = _translate(REPOSITORY_BOOTSTRAP_DEST_SPECIAL)
        installing = destination + ".installing"
        backup = destination + ".previous"
        if os.path.exists(installing):
            shutil.rmtree(installing)
        if os.path.exists(backup):
            return {"ok": False, "error_code": "STALE_BACKUP_PRESENT"}, 409
        if action == "installed" and os.path.exists(destination):
            return {"ok": False, "error_code": "DESTINATION_ALREADY_EXISTS"}, 409
        if action == "upgraded" and not os.path.isdir(destination):
            return {"ok": False, "error_code": "INSTALLED_PATH_MISSING"}, 409

        try:
            os.makedirs(installing)
            with zipfile.ZipFile(artifact["translated_path"], "r") as archive:
                prefix = REPOSITORY_BOOTSTRAP_ADDON_ID + "/"
                for member in sorted(REPOSITORY_BOOTSTRAP_MEMBERS):
                    relative = member[len(prefix):]
                    output = os.path.join(installing, relative)
                    parent = os.path.dirname(output)
                    if parent and not os.path.exists(parent):
                        os.makedirs(parent)
                    with archive.open(member, "r") as source, open(output, "wb") as target:
                        shutil.copyfileobj(source, target, 64 * 1024)
            if action == "upgraded":
                os.rename(destination, backup)
            try:
                os.rename(installing, destination)
            except Exception:
                if os.path.exists(backup) and not os.path.exists(destination):
                    os.rename(backup, destination)
                raise
        except Exception as exc:
            if os.path.exists(installing):
                shutil.rmtree(installing)
            return {
                "ok": False,
                "error_code": "INSTALL_FAILED",
                "message": "failed to install canonical repository bootstrap: %s" % exc,
            }, 500

        def restore_previous_filesystem():
            if os.path.exists(destination):
                shutil.rmtree(destination)
            if os.path.exists(backup):
                os.rename(backup, destination)
            xbmc.executebuiltin("UpdateLocalAddons", wait=True)

        xbmc.executebuiltin("UpdateLocalAddons", wait=True)
        deadline = time.time() + 5
        after = None
        while time.time() < deadline:
            after, _ = self._get_addon_info(REPOSITORY_BOOTSTRAP_ADDON_ID)
            if after.get("installed") and after.get("version") == artifact_version:
                break
            time.sleep(0.25)
        if not isinstance(after, dict) or not after.get("installed"):
            restore_previous_filesystem()
            return {"ok": False, "error_code": "ADDON_NOT_DISCOVERED"}, 500

        response, error = self._jsonrpc(
            "Addons.SetAddonEnabled",
            {"addonid": REPOSITORY_BOOTSTRAP_ADDON_ID, "enabled": True},
        )
        if error or (isinstance(response, dict) and response.get("error")):
            restore_previous_filesystem()
            return {"ok": False, "error_code": "ENABLE_FAILED"}, 500
        xbmc.executebuiltin("UpdateAddonRepos", wait=False)
        after, _ = self._get_addon_info(REPOSITORY_BOOTSTRAP_ADDON_ID)
        if after.get("version") != artifact_version or not after.get("enabled"):
            restore_previous_filesystem()
            return {"ok": False, "error_code": "POST_INSTALL_VERIFICATION_FAILED"}, 500
        if os.path.exists(backup):
            shutil.rmtree(backup)
        return {
            "ok": True,
            "action": action,
            "addon_id": REPOSITORY_BOOTSTRAP_ADDON_ID,
            "version": artifact_version,
            "enabled": True,
            "artifact_sha256": artifact.get("sha256"),
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
            self._write_json(self._get_shallow_health())
            return

        if parsed.path == "/health/deep":
            payload, status = self._get_deep_health()
            self._write_json(payload, status=status)
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
                    "build": self._get_build_identity(),
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
                        "repository_bootstrap_install": {
                            "method": "POST",
                            "path": "/repo/bootstrap/install",
                            "auth_required": True,
                            "auth_header": AUTH_HEADER_TOKEN,
                            "caller_arguments": [],
                        },
                        "repository_readiness": {
                            "method": "GET",
                            "path": "/repo/readiness",
                            "auth_required": True,
                            "auth_header": AUTH_HEADER_TOKEN,
                            "caller_arguments": [],
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
                        "gui_state": {
                            "method": "GET",
                            "path": "/gui/state",
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
                        "repository_bootstrap_install": True,
                        "repository_readiness": True,
                        "gui_actions": sorted(GUI_ACTIONS.keys()),
                        "gui_state": True,
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
                    "build": self._get_build_identity(),
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

        if parsed.path == "/gui/state":
            result, status = self._gui_state()
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

        if parsed.path == "/repo/readiness":
            if parsed.query:
                self._write_envelope(
                    {"ok": False, "error_code": "ARGUMENTS_FORBIDDEN"}, status=400
                )
                return
            result, status = self._repository_readiness()
            self._write_envelope(result, status=status)
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

        if parsed.path == "/gui/subtitle":
            payload, error = self._read_json_body()
            if error:
                self._write_json({"error": error}, status=400)
                return
            payload = payload or {}
            lines = payload.get("lines")
            if not isinstance(lines, list):
                self._write_json({"error": "lines must be a list"}, status=400)
                return
            ttl = float(payload.get("ttl", subtitle_overlay.DEFAULT_TTL) or 0)
            ok = subtitle_overlay.get_overlay().show(lines, ttl=ttl)
            self._write_json({"status": "ok" if ok else "error", "visible": ok})
            return

        if parsed.path == "/gui/subtitle/clear":
            subtitle_overlay.get_overlay().hide()
            self._write_json({"status": "ok", "visible": False})
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

        if parsed.path == "/repo/bootstrap/install":
            if not self._require_token_auth():
                return
            if parsed.query:
                self._write_envelope(
                    {"ok": False, "error_code": "ARGUMENTS_FORBIDDEN"}, status=400
                )
                return
            body, body_error = self._read_json_body()
            if body_error or not isinstance(body, dict) or body:
                self._write_envelope(
                    {"ok": False, "error_code": "ARGUMENTS_FORBIDDEN"}, status=400
                )
                return
            result, status = self._install_repository_bootstrap()
            self._write_envelope(result, status=status)
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
