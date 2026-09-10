from __future__ import annotations

import builtins
import hashlib
import importlib
import json
import os
import sys
import types

import pytest


def _bridge_module():
    xbmc = sys.modules.setdefault("xbmc", types.ModuleType("xbmc"))
    xbmc.log = lambda *args, **kwargs: None
    xbmc.LOGDEBUG = 0
    xbmc.LOGINFO = 1
    xbmc.LOGWARNING = 2
    xbmc.LOGERROR = 3
    xbmcaddon = sys.modules.setdefault("xbmcaddon", types.ModuleType("xbmcaddon"))
    xbmcaddon.Addon = lambda *args, **kwargs: None
    sys.modules.setdefault("xbmcgui", types.ModuleType("xbmcgui"))
    xbmcvfs = sys.modules.setdefault("xbmcvfs", types.ModuleType("xbmcvfs"))
    xbmcvfs.translatePath = lambda path: path
    xbmcvfs.exists = os.path.exists
    return importlib.import_module("http_bridge")


def test_absent_staged_repository_is_successful_readback(tmp_path, monkeypatch):
    bridge = _bridge_module()
    staged_path = tmp_path / "dev-repo.zip"
    monkeypatch.setattr(bridge, "_translate", lambda value: str(staged_path))
    monkeypatch.setattr(bridge, "load_state", lambda: {"schema_version": 1, "state_rev": 0})
    handler = object.__new__(bridge.KodiBridgeHandler)

    result, status = handler._staged_repository_status()

    assert status == 200
    assert result == {
        "ok": True,
        "exists": False,
        "size_bytes": None,
        "sha256": None,
        "metadata_present": False,
        "metadata_consistent": False,
    }


def test_present_staged_repository_reports_actual_bytes(tmp_path, monkeypatch):
    bridge = _bridge_module()
    staged_path = tmp_path / "dev-repo.zip"
    staged_path.write_bytes(b"fixed staged repository bytes")
    monkeypatch.setattr(bridge, "_translate", lambda value: str(staged_path))
    monkeypatch.setattr(bridge, "load_state", lambda: {"schema_version": 1, "state_rev": 0})
    handler = object.__new__(bridge.KodiBridgeHandler)

    result, status = handler._staged_repository_status()

    assert status == 200
    assert result == {
        "ok": True,
        "exists": True,
        "size_bytes": 29,
        "sha256": "3cad3e8881b2d91956fed68215a11121b382009456c2f899d158eceaf5e590e1",
        "metadata_present": False,
        "metadata_consistent": False,
    }


def _state_for(bridge, body, *, sha256=None, size_bytes=None):
    return {
        "schema_version": 1,
        "state_rev": 4,
        "repo_zip": {
            "repo_id": bridge.REPOSITORY_BOOTSTRAP_REPO_ID,
            "repo_version": "1.0.4",
            "special_path": bridge.REPOSITORY_BOOTSTRAP_SPECIAL_PATH,
            "size_bytes": len(body) if size_bytes is None else size_bytes,
            "sha256": hashlib.sha256(body).hexdigest() if sha256 is None else sha256,
            "staged_at": 123456,
        },
    }


def test_exact_persisted_metadata_is_consistent(tmp_path, monkeypatch):
    bridge = _bridge_module()
    body = b"metadata exact"
    staged_path = tmp_path / "dev-repo.zip"
    staged_path.write_bytes(body)
    monkeypatch.setattr(bridge, "_translate", lambda value: str(staged_path))
    monkeypatch.setattr(bridge, "load_state", lambda: _state_for(bridge, body))
    handler = object.__new__(bridge.KodiBridgeHandler)

    result, status = handler._staged_repository_status()

    assert status == 200
    assert result["sha256"] == hashlib.sha256(body).hexdigest()
    assert result["size_bytes"] == len(body)
    assert result["metadata_present"] is True
    assert result["metadata_consistent"] is True


def test_metadata_mismatch_returns_actual_bytes_without_mutation(tmp_path, monkeypatch):
    bridge = _bridge_module()
    body = b"actual target bytes"
    staged_path = tmp_path / "dev-repo.zip"
    staged_path.write_bytes(body)
    monkeypatch.setattr(bridge, "_translate", lambda value: str(staged_path))
    monkeypatch.setattr(
        bridge,
        "load_state",
        lambda: _state_for(bridge, body, sha256="0" * 64, size_bytes=len(body) + 1),
    )
    monkeypatch.setattr(bridge, "save_state", lambda *args: (_ for _ in ()).throw(AssertionError("write")))
    for name in ("mkdirs", "delete", "rename"):
        monkeypatch.setattr(
            bridge.xbmcvfs,
            name,
            lambda *args, _name=name, **kwargs: (_ for _ in ()).throw(AssertionError(_name)),
            raising=False,
        )
    before = {
        path.name: (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in tmp_path.iterdir()
    }
    handler = object.__new__(bridge.KodiBridgeHandler)
    tripwire = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("mutation"))
    monkeypatch.setattr(bridge.xbmc, "executebuiltin", tripwire, raising=False)
    monkeypatch.setattr(handler, "_jsonrpc", tripwire)
    monkeypatch.setattr(handler, "_install_repository_bootstrap", tripwire)

    result, status = handler._staged_repository_status()

    after = {
        path.name: (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in tmp_path.iterdir()
    }
    assert status == 200
    assert result["sha256"] == hashlib.sha256(body).hexdigest()
    assert result["size_bytes"] == len(body)
    assert result["metadata_present"] is True
    assert result["metadata_consistent"] is False
    assert after == before


@pytest.mark.parametrize(
    ("metadata_override", "case_name"),
    (
        ({"size_bytes": True}, "boolean size"),
        ({"size_bytes": "1"}, "string size"),
        ({"sha256": 1234}, "non-string sha"),
        ({"repo_id": 1234}, "non-string repo id"),
        ({"special_path": ["dev-repo.zip"]}, "non-string special path"),
    ),
)
def test_semantically_invalid_metadata_types_are_inconsistent_and_read_only(
    tmp_path, monkeypatch, metadata_override, case_name
):
    bridge = _bridge_module()
    body = b"x"
    staged_path = tmp_path / "dev-repo.zip"
    staged_path.write_bytes(body)
    state = _state_for(bridge, body)
    state["repo_zip"].update(metadata_override)
    monkeypatch.setattr(bridge, "_translate", lambda value: str(staged_path))
    monkeypatch.setattr(bridge, "load_state", lambda: state)
    monkeypatch.setattr(
        bridge,
        "save_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("write")),
    )
    handler = object.__new__(bridge.KodiBridgeHandler)

    result, status = handler._staged_repository_status()

    assert status == 200, case_name
    assert result["exists"] is True
    assert result["size_bytes"] == len(body)
    assert result["sha256"] == hashlib.sha256(body).hexdigest()
    assert result["metadata_present"] is True
    assert result["metadata_consistent"] is False
    assert staged_path.read_bytes() == body


def test_get_route_is_authenticated_zero_argument_readback(monkeypatch):
    bridge = _bridge_module()
    handler = object.__new__(bridge.KodiBridgeHandler)
    handler.path = "/repo/staged/status"
    captured = {}
    auth_calls = 0
    expected = {
        "ok": True,
        "exists": False,
        "size_bytes": None,
        "sha256": None,
        "metadata_present": False,
        "metadata_consistent": False,
    }

    def authenticate():
        nonlocal auth_calls
        auth_calls += 1
        return True

    monkeypatch.setattr(handler, "_require_token_auth", authenticate)
    monkeypatch.setattr(handler, "_staged_repository_status", lambda: (expected, 200))
    monkeypatch.setattr(
        handler,
        "_write_envelope",
        lambda payload, status=200: captured.update(payload=payload, status=status),
    )

    handler.do_GET()

    assert auth_calls == 1
    assert captured == {"payload": expected, "status": 200}


def _configure_token_auth(handler, monkeypatch, configured_token):
    class Addon:
        def getSetting(self, key):
            assert key == "mcp_token"
            return configured_token

    monkeypatch.setattr(handler, "_get_addon", lambda: Addon())


def test_status_route_rejects_missing_credentials_when_token_is_configured(monkeypatch):
    bridge = _bridge_module()
    handler = object.__new__(bridge.KodiBridgeHandler)
    handler.path = "/repo/staged/status"
    handler.headers = {}
    captured = {}
    _configure_token_auth(handler, monkeypatch, "configured-token")
    monkeypatch.setattr(
        handler,
        "_staged_repository_status",
        lambda: (_ for _ in ()).throw(AssertionError("unauthorized readback")),
    )
    monkeypatch.setattr(
        handler,
        "_write_envelope",
        lambda payload, status=200: captured.update(payload=payload, status=status),
    )

    handler.do_GET()

    assert captured["status"] == 401
    assert captured["payload"]["error_code"] == "UNAUTHORIZED"


def test_status_route_rejects_incorrect_credentials_when_token_is_configured(monkeypatch):
    bridge = _bridge_module()
    handler = object.__new__(bridge.KodiBridgeHandler)
    handler.path = "/repo/staged/status"
    handler.headers = {bridge.AUTH_HEADER_TOKEN: "incorrect-token"}
    captured = {}
    _configure_token_auth(handler, monkeypatch, "configured-token")
    monkeypatch.setattr(
        handler,
        "_staged_repository_status",
        lambda: (_ for _ in ()).throw(AssertionError("unauthorized readback")),
    )
    monkeypatch.setattr(
        handler,
        "_write_envelope",
        lambda payload, status=200: captured.update(payload=payload, status=status),
    )

    handler.do_GET()

    assert captured["status"] == 401
    assert captured["payload"]["error_code"] == "UNAUTHORIZED"


def test_status_route_executes_with_correct_credentials_when_token_is_configured(monkeypatch):
    bridge = _bridge_module()
    handler = object.__new__(bridge.KodiBridgeHandler)
    handler.path = "/repo/staged/status"
    handler.headers = {bridge.AUTH_HEADER_TOKEN: "configured-token"}
    captured = {}
    expected = {
        "ok": True,
        "exists": False,
        "size_bytes": None,
        "sha256": None,
        "metadata_present": False,
        "metadata_consistent": False,
    }
    _configure_token_auth(handler, monkeypatch, "configured-token")
    monkeypatch.setattr(handler, "_staged_repository_status", lambda: (expected, 200))
    monkeypatch.setattr(
        handler,
        "_write_envelope",
        lambda payload, status=200: captured.update(payload=payload, status=status),
    )

    handler.do_GET()

    assert captured == {"payload": expected, "status": 200}


def test_get_route_rejects_query_arguments(monkeypatch):
    bridge = _bridge_module()
    handler = object.__new__(bridge.KodiBridgeHandler)
    handler.path = "/repo/staged/status?path=/home/user/.kodi/repository.zip"
    captured = {}
    monkeypatch.setattr(handler, "_require_token_auth", lambda: True)
    monkeypatch.setattr(
        handler,
        "_staged_repository_status",
        lambda: (_ for _ in ()).throw(AssertionError("readback must not run")),
    )
    monkeypatch.setattr(
        handler,
        "_write_envelope",
        lambda payload, status=200: captured.update(payload=payload, status=status),
    )

    handler.do_GET()

    assert captured == {
        "payload": {"ok": False, "error_code": "ARGUMENTS_FORBIDDEN"},
        "status": 400,
    }


def test_capabilities_advertise_authenticated_read_only_status(monkeypatch):
    bridge = _bridge_module()
    handler = object.__new__(bridge.KodiBridgeHandler)
    handler.path = "/capabilities"
    captured = {}

    class Addon:
        def getAddonInfo(self, key):
            return "service.kodi_mcp"

    monkeypatch.setattr(bridge.xbmcaddon, "Addon", Addon)
    monkeypatch.setattr(handler, "_write_json", lambda payload, status=200: captured.update(payload))

    handler.do_GET()

    assert captured["endpoints"]["repo_staged_status"] == {
        "method": "GET",
        "path": "/repo/staged/status",
        "auth_required": True,
        "auth_header": bridge.AUTH_HEADER_TOKEN,
        "caller_arguments": [],
    }


def test_unreadable_staged_repository_returns_sanitized_error(monkeypatch):
    bridge = _bridge_module()
    target_path = "/home/user/.kodi/userdata/addon_data/service.kodi_mcp/repository.zip"
    monkeypatch.setattr(bridge, "_translate", lambda value: target_path)
    monkeypatch.setattr(bridge, "load_state", lambda: _state_for(bridge, b"expected"))
    monkeypatch.setattr(bridge.xbmcvfs, "exists", lambda path: True)
    monkeypatch.setattr(
        bridge,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("denied: " + target_path)),
        raising=False,
    )
    handler = object.__new__(bridge.KodiBridgeHandler)

    result, status = handler._staged_repository_status()

    assert status == 500
    assert result == {
        "ok": False,
        "error_code": "STAGED_REPOSITORY_READ_FAILED",
        "message": "staged repository artifact could not be read",
    }
    serialized = json.dumps(result, sort_keys=True)
    assert target_path not in serialized
    for secret in ("special://", "127.0.0.1:8765", "X-Kodi-MCP-Token", "secret-token"):
        assert secret not in serialized


def test_staged_repository_hashing_is_bounded_by_upload_limit(tmp_path, monkeypatch):
    bridge = _bridge_module()
    staged_path = tmp_path / "dev-repo.zip"
    staged_path.write_bytes(b"12345")
    monkeypatch.setattr(bridge, "MAX_REPO_ZIP_UPLOAD_BYTES", 4)
    monkeypatch.setattr(bridge, "_translate", lambda value: str(staged_path))
    monkeypatch.setattr(bridge, "load_state", lambda: {"schema_version": 1, "state_rev": 0})
    handler = object.__new__(bridge.KodiBridgeHandler)

    result, status = handler._staged_repository_status()

    assert status == 500
    assert result == {
        "ok": False,
        "error_code": "STAGED_REPOSITORY_TOO_LARGE",
        "message": "staged repository artifact exceeds the supported size limit",
    }


def test_replacement_during_read_is_detected_from_one_open_descriptor(tmp_path, monkeypatch):
    bridge = _bridge_module()
    old_body = b"opened descriptor bytes"
    new_body = b"replacement path bytes"
    staged_path = tmp_path / "dev-repo.zip"
    replacement_path = tmp_path / "replacement.zip"
    staged_path.write_bytes(old_body)
    replacement_path.write_bytes(new_body)
    monkeypatch.setattr(bridge, "_translate", lambda value: str(staged_path))
    monkeypatch.setattr(bridge, "load_state", lambda: {"schema_version": 1, "state_rev": 0})
    observed_chunks = []

    class ReplacingReader:
        def __init__(self):
            self.handle = builtins.open(staged_path, "rb")
            self.replaced = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, size):
            if not self.replaced:
                os.replace(replacement_path, staged_path)
                self.replaced = True
            chunk = self.handle.read(size)
            observed_chunks.append(chunk)
            return chunk

    monkeypatch.setattr(bridge, "open", lambda *args, **kwargs: ReplacingReader(), raising=False)
    handler = object.__new__(bridge.KodiBridgeHandler)

    result, status = handler._staged_repository_status()

    assert status == 409
    assert result == {
        "ok": False,
        "error_code": "STAGED_REPOSITORY_CHANGED_DURING_READ",
        "message": "staged repository artifact changed during read",
    }
    assert b"".join(observed_chunks) == old_body
    assert staged_path.read_bytes() == new_body
