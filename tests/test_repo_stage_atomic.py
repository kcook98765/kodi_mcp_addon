from __future__ import annotations

import ast
import copy
import errno
import hashlib
import importlib
import inspect
import io
import json
import os
import stat
import sys
import threading
import types

import pytest


class _KodiVfsFile:
    def __init__(self, path, mode="r"):
        self._handle = open(path, mode)

    def read(self, size=-1):
        return self._handle.read(size)

    def write(self, body):
        return self._handle.write(body) == len(body)

    def close(self):
        self._handle.close()


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
    sys.modules.setdefault("xbmcvfs", types.ModuleType("xbmcvfs"))
    return importlib.import_module("http_bridge")


def _configure_local_vfs(bridge, root, monkeypatch):
    repo_dir = root / "dev_repo"

    def translate(path):
        if path.startswith(bridge.DEV_REPO_DIR_SPECIAL):
            suffix = path[len(bridge.DEV_REPO_DIR_SPECIAL) :].lstrip("/")
            return str(repo_dir / suffix) if suffix else str(repo_dir)
        return str(root / os.path.basename(path))

    def delete(path):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return False

    def rename(source, destination):
        os.rename(source, destination)
        return True

    monkeypatch.setattr(bridge.xbmcvfs, "translatePath", translate, raising=False)
    monkeypatch.setattr(bridge.xbmcvfs, "exists", os.path.exists, raising=False)
    monkeypatch.setattr(bridge.xbmcvfs, "mkdirs", lambda path: os.makedirs(path, exist_ok=True), raising=False)
    monkeypatch.setattr(bridge.xbmcvfs, "File", _KodiVfsFile, raising=False)
    monkeypatch.setattr(bridge.xbmcvfs, "delete", delete, raising=False)
    monkeypatch.setattr(bridge.xbmcvfs, "rename", rename, raising=False)
    return repo_dir


def _state_store(bridge, monkeypatch):
    stored = {"schema_version": bridge.STATE_SCHEMA_VERSION, "state_rev": 0}

    def load():
        return copy.deepcopy(stored)

    def save(value):
        value = copy.deepcopy(value)
        value["state_rev"] = int(value.get("state_rev") or 0) + 1
        stored.clear()
        stored.update(value)
        return copy.deepcopy(stored)

    monkeypatch.setattr(bridge, "load_state", load)
    monkeypatch.setattr(bridge, "save_state", save)
    return stored


def _stage_handler(bridge, body, mode="fail_if_exists"):
    if hasattr(bridge, "ATOMIC_LINK_SUPPORTED"):
        bridge._ATOMIC_FAIL_IF_EXISTS_CACHE = bridge.ATOMIC_LINK_SUPPORTED
    handler = object.__new__(bridge.KodiBridgeHandler)
    handler.path = "/repo/stage?repo_id=dev-repo&mode=%s" % mode
    handler.headers = {
        "Content-Type": "application/zip",
        "Content-Length": str(len(body)),
        "X-Content-SHA256": hashlib.sha256(body).hexdigest(),
        "X-Repo-Version": "1.0.0",
    }
    handler.rfile = io.BytesIO(body)
    handler._require_token_auth = lambda: True
    captured = {}
    handler._write_envelope = lambda payload, status=200: captured.update(
        payload=payload, status=status
    )
    return handler, captured


def _repo_metadata(bridge, body):
    return {
        "repo_id": bridge.REPOSITORY_BOOTSTRAP_REPO_ID,
        "repo_version": "1.0.0",
        "special_path": bridge.REPOSITORY_BOOTSTRAP_SPECIAL_PATH,
        "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "staged_at": 1,
    }


def _register_handler(bridge):
    payload = {
        "control_api_version": 1,
        "server_id": "server",
        "server_instance_id": "instance",
        "server_base_url": "http://server.test",
        "ttl_seconds": 60,
        "features": {"repo_zip_staging": True},
    }
    body = json.dumps(payload).encode("utf-8")
    handler = object.__new__(bridge.KodiBridgeHandler)
    handler.path = "/mcp/register"
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler._require_token_auth = lambda: True
    captured = {}
    handler._write_envelope = lambda result, status=200: captured.update(
        payload=result, status=status
    )
    return handler, captured


def _capabilities_handler(bridge, monkeypatch):
    handler = object.__new__(bridge.KodiBridgeHandler)
    handler.path = "/capabilities"
    captured = {}

    class Addon:
        def getAddonInfo(self, key):
            return "service.kodi_mcp"

    monkeypatch.setattr(bridge.xbmcaddon, "Addon", Addon)
    handler._write_json = lambda payload, status=200: captured.update(payload)
    return handler, captured


def test_conditional_stage_does_not_replace_slot_created_after_initial_check(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    stored = _state_store(bridge, monkeypatch)
    conditional_body = b"conditional request bytes"
    competing_body = b"competing bridge stage bytes"
    handler, captured = _stage_handler(bridge, conditional_body)
    competing_handler, competing_result = _stage_handler(
        bridge, competing_body, mode="overwrite"
    )
    final_path = repo_dir / "dev-repo.zip"
    competing_state = []

    class CompetingStageOnFirstRead(io.BytesIO):
        def __init__(self, body):
            super().__init__(body)
            self.triggered = False

        def read(self, size=-1):
            if not self.triggered:
                self.triggered = True
                competing_handler.do_POST()
                competing_state.append(copy.deepcopy(stored))
            return super().read(size)

    handler.rfile = CompetingStageOnFirstRead(conditional_body)

    handler.do_POST()

    assert competing_result["payload"]["ok"] is True
    assert captured == {
        "payload": {
            "ok": False,
            "error_code": "ALREADY_EXISTS",
            "message": "repo zip already staged",
        },
        "status": 200,
    }
    assert final_path.read_bytes() == competing_body
    assert stored == competing_state[0]
    assert list(repo_dir.glob("*.tmp*")) == []


def test_conditional_stage_absent_succeeds_and_persists_matching_metadata(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    stored = _state_store(bridge, monkeypatch)
    body = b"first conditional bytes"
    handler, captured = _stage_handler(bridge, body)

    handler.do_POST()

    assert captured["payload"]["ok"] is True
    assert (repo_dir / "dev-repo.zip").read_bytes() == body
    assert stored["repo_zip"]["size_bytes"] == len(body)
    assert stored["repo_zip"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert list(repo_dir.glob("*.tmp*")) == []


def test_conditional_stage_existing_refuses_without_reading_or_mutating(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    stored = _state_store(bridge, monkeypatch)
    existing_body = b"existing bytes"
    final_path = repo_dir / "dev-repo.zip"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(existing_body)
    stored["repo_zip"] = _repo_metadata(bridge, existing_body)
    before_state = copy.deepcopy(stored)
    handler, captured = _stage_handler(bridge, b"must not be read")

    class UnreadableBody:
        def read(self, size=-1):
            raise AssertionError("body was read")

    handler.rfile = UnreadableBody()

    handler.do_POST()

    assert captured["payload"]["error_code"] == "ALREADY_EXISTS"
    assert final_path.read_bytes() == existing_body
    assert stored == before_state
    assert list(repo_dir.glob("*.tmp*")) == []


def test_two_concurrent_conditional_stages_have_one_winner(tmp_path, monkeypatch):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    stored = _state_store(bridge, monkeypatch)
    barrier = threading.Barrier(2)
    bodies = (b"conditional one", b"conditional two")
    handlers = []
    captures = []

    class BarrierBody(io.BytesIO):
        def __init__(self, body):
            super().__init__(body)
            self.waited = False

        def read(self, size=-1):
            chunk = super().read(size)
            if not self.waited:
                self.waited = True
                barrier.wait(timeout=5)
            return chunk

    for body in bodies:
        handler, captured = _stage_handler(bridge, body)
        handler.rfile = BarrierBody(body)
        handlers.append(handler)
        captures.append(captured)

    threads = [threading.Thread(target=handler.do_POST) for handler in handlers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(capture["payload"]["ok"] for capture in captures) == [False, True]
    loser = next(capture for capture in captures if not capture["payload"]["ok"])
    assert loser["payload"]["error_code"] == "ALREADY_EXISTS"
    final_body = (repo_dir / "dev-repo.zip").read_bytes()
    assert final_body in bodies
    assert stored["repo_zip"]["sha256"] == hashlib.sha256(final_body).hexdigest()
    assert stored["repo_zip"]["size_bytes"] == len(final_body)
    assert list(repo_dir.glob("*.tmp*")) == []


def test_overwrite_wins_while_conditional_upload_is_in_progress(tmp_path, monkeypatch):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    stored = _state_store(bridge, monkeypatch)
    conditional_body = b"conditional pending"
    overwrite_body = b"overwrite winner"
    conditional, conditional_result = _stage_handler(bridge, conditional_body)
    overwrite, overwrite_result = _stage_handler(bridge, overwrite_body, mode="overwrite")
    upload_started = threading.Event()
    allow_upload = threading.Event()

    class BlockedBody(io.BytesIO):
        def read(self, size=-1):
            upload_started.set()
            assert allow_upload.wait(timeout=5)
            return super().read(size)

    conditional.rfile = BlockedBody(conditional_body)
    thread = threading.Thread(target=conditional.do_POST)
    thread.start()
    assert upload_started.wait(timeout=5)

    overwrite.do_POST()
    allow_upload.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert overwrite_result["payload"]["ok"] is True
    assert conditional_result["payload"]["error_code"] == "ALREADY_EXISTS"
    assert (repo_dir / "dev-repo.zip").read_bytes() == overwrite_body
    assert stored["repo_zip"]["sha256"] == hashlib.sha256(overwrite_body).hexdigest()
    assert list(repo_dir.glob("*.tmp*")) == []


def test_conditional_wins_before_in_progress_overwrite_replaces_it(tmp_path, monkeypatch):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    stored = _state_store(bridge, monkeypatch)
    conditional_body = b"conditional first"
    overwrite_body = b"overwrite second"
    conditional, conditional_result = _stage_handler(bridge, conditional_body)
    overwrite, overwrite_result = _stage_handler(bridge, overwrite_body, mode="overwrite")
    upload_started = threading.Event()
    allow_upload = threading.Event()

    class BlockedBody(io.BytesIO):
        def read(self, size=-1):
            upload_started.set()
            assert allow_upload.wait(timeout=5)
            return super().read(size)

    overwrite.rfile = BlockedBody(overwrite_body)
    thread = threading.Thread(target=overwrite.do_POST)
    thread.start()
    assert upload_started.wait(timeout=5)

    conditional.do_POST()
    allow_upload.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert conditional_result["payload"]["ok"] is True
    assert overwrite_result["payload"]["ok"] is True
    assert (repo_dir / "dev-repo.zip").read_bytes() == overwrite_body
    assert stored["repo_zip"]["sha256"] == hashlib.sha256(overwrite_body).hexdigest()
    assert list(repo_dir.glob("*.tmp*")) == []


def test_atomic_link_refuses_direct_filesystem_creation_at_activation(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    stored = _state_store(bridge, monkeypatch)
    conditional_body = b"conditional bytes"
    external_body = b"external direct writer"
    handler, captured = _stage_handler(bridge, conditional_body)
    final_path = repo_dir / "dev-repo.zip"
    original_link = os.link

    def create_then_link(source, destination):
        final_path.write_bytes(external_body)
        stored["repo_zip"] = _repo_metadata(bridge, external_body)
        original_link(source, destination)

    monkeypatch.setattr(bridge.os, "link", create_then_link)

    handler.do_POST()

    assert captured["payload"]["error_code"] == "ALREADY_EXISTS"
    assert final_path.read_bytes() == external_body
    assert stored["repo_zip"] == _repo_metadata(bridge, external_body)
    assert list(repo_dir.glob("*.tmp*")) == []


def test_upload_error_cleans_private_temp(tmp_path, monkeypatch):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    _state_store(bridge, monkeypatch)
    handler, captured = _stage_handler(bridge, b"upload failure")

    class BrokenBody:
        def read(self, size=-1):
            raise ConnectionError("client disconnected")

    handler.rfile = BrokenBody()

    handler.do_POST()

    assert captured["payload"]["error_code"] == "UPLOAD_FAILED"
    assert not (repo_dir / "dev-repo.zip").exists()
    assert list(repo_dir.glob("*.tmp*")) == []


def test_activation_error_releases_lock_and_cleans_temp(tmp_path, monkeypatch):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    _state_store(bridge, monkeypatch)
    first, first_result = _stage_handler(bridge, b"first attempt")
    original_link = os.link
    attempts = 0

    def fail_once(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("activation failed")
        return original_link(source, destination)

    monkeypatch.setattr(bridge.os, "link", fail_once)
    first.do_POST()

    second_body = b"second attempt"
    second, second_result = _stage_handler(bridge, second_body)
    second.do_POST()

    assert first_result["payload"]["error_code"] == "STAGE_FAILED"
    assert second_result["payload"]["ok"] is True
    assert (repo_dir / "dev-repo.zip").read_bytes() == second_body
    assert list(repo_dir.glob("*.tmp*")) == []


def test_metadata_failure_after_activation_preserves_activated_zip(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    previous_state = {
        "schema_version": bridge.STATE_SCHEMA_VERSION,
        "state_rev": 8,
        "repo_zip": _repo_metadata(bridge, b"previous metadata bytes"),
    }
    monkeypatch.setattr(bridge, "load_state", lambda: copy.deepcopy(previous_state))
    monkeypatch.setattr(
        bridge,
        "save_state",
        lambda value: (_ for _ in ()).throw(OSError("metadata write failed")),
    )
    body = b"activated before metadata failure"
    handler, captured = _stage_handler(bridge, body)

    handler.do_POST()

    assert captured["payload"]["error_code"] == "STAGE_FAILED"
    assert (repo_dir / "dev-repo.zip").read_bytes() == body
    assert bridge.load_state() == previous_state
    assert list(repo_dir.glob("*.tmp*")) == []


def test_checksum_and_incomplete_upload_validation_remain_non_mutating(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    stored = _state_store(bridge, monkeypatch)
    body = b"validation bytes"
    checksum_handler, checksum_result = _stage_handler(bridge, body)
    checksum_handler.headers["X-Content-SHA256"] = "0" * 64

    checksum_handler.do_POST()

    incomplete_handler, incomplete_result = _stage_handler(bridge, body)
    incomplete_handler.headers["Content-Length"] = str(len(body) + 1)
    incomplete_handler.do_POST()

    assert checksum_result["payload"]["error_code"] == "CHECKSUM_MISMATCH"
    assert incomplete_result["payload"]["error_code"] == "INCOMPLETE_UPLOAD"
    assert stored == {"schema_version": bridge.STATE_SCHEMA_VERSION, "state_rev": 0}
    assert not (repo_dir / "dev-repo.zip").exists()
    assert list(repo_dir.glob("*.tmp*")) == []


def test_upload_limit_refusal_does_not_read_body(tmp_path, monkeypatch):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    _state_store(bridge, monkeypatch)
    handler, captured = _stage_handler(bridge, b"x")
    handler.headers["Content-Length"] = str(bridge.MAX_REPO_ZIP_UPLOAD_BYTES + 1)

    class UnreadableBody:
        def read(self, size=-1):
            raise AssertionError("oversized body was read")

    handler.rfile = UnreadableBody()
    handler.do_POST()

    assert captured["status"] == 413
    assert captured["payload"]["error_code"] == "PAYLOAD_TOO_LARGE"
    assert not repo_dir.exists()


def test_overwrite_replaces_existing_bytes_and_metadata(tmp_path, monkeypatch):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    stored = _state_store(bridge, monkeypatch)
    old_body = b"old bytes"
    new_body = b"new overwrite bytes"
    final_path = repo_dir / "dev-repo.zip"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(old_body)
    stored["repo_zip"] = _repo_metadata(bridge, old_body)
    handler, captured = _stage_handler(bridge, new_body, mode="overwrite")

    handler.do_POST()

    assert captured["payload"]["ok"] is True
    assert final_path.read_bytes() == new_body
    assert stored["repo_zip"]["sha256"] == hashlib.sha256(new_body).hexdigest()
    assert stored["repo_zip"]["size_bytes"] == len(new_body)
    assert list(repo_dir.glob("*.tmp*")) == []


def test_stage_route_authentication_is_unchanged(tmp_path, monkeypatch):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    _state_store(bridge, monkeypatch)
    body = b"unauthorized bytes"
    handler = object.__new__(bridge.KodiBridgeHandler)
    handler.path = "/repo/stage?repo_id=dev-repo&mode=fail_if_exists"
    handler.headers = {
        "Content-Type": "application/zip",
        "Content-Length": str(len(body)),
    }
    handler.rfile = io.BytesIO(body)
    captured = {}

    class Addon:
        def getSetting(self, key):
            assert key == "mcp_token"
            return "configured-token"

    handler._get_addon = lambda: Addon()
    handler._write_envelope = lambda payload, status=200: captured.update(
        payload=payload, status=status
    )

    handler.do_POST()

    assert captured["status"] == 401
    assert captured["payload"]["error_code"] == "UNAUTHORIZED"
    assert handler.rfile.tell() == 0
    assert not repo_dir.exists()


def test_capabilities_advertise_atomic_conditional_stage(monkeypatch):
    bridge = _bridge_module()
    handler = object.__new__(bridge.KodiBridgeHandler)
    handler.path = "/capabilities"
    captured = {}

    class Addon:
        def getAddonInfo(self, key):
            return "service.kodi_mcp"

    monkeypatch.setattr(bridge.xbmcaddon, "Addon", Addon)
    monkeypatch.setattr(
        bridge,
        "_atomic_fail_if_exists_status",
        lambda: bridge.ATOMIC_LINK_SUPPORTED,
    )
    handler._write_json = lambda payload, status=200: captured.update(payload)

    handler.do_GET()

    assert captured["endpoints"]["repo_stage"] == {
        "method": "POST",
        "path": "/repo/stage",
        "auth_required": True,
        "auth_header": bridge.AUTH_HEADER_TOKEN,
        "modes": ["overwrite", "fail_if_exists"],
        "atomic_fail_if_exists": True,
    }


def test_registration_cannot_overwrite_staged_metadata_with_stale_state(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    stored = {"schema_version": bridge.STATE_SCHEMA_VERSION, "state_rev": 0}
    register_loaded = threading.Event()
    allow_register = threading.Event()
    stage_activated = threading.Event()
    stage_waiting_for_state = threading.Event()

    class ObservedStateLock:
        def __init__(self):
            self.lock = threading.RLock()

        def __enter__(self):
            if threading.current_thread().name == "stage":
                stage_waiting_for_state.set()
            self.lock.acquire()
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.lock.release()

    def load():
        snapshot = copy.deepcopy(stored)
        if threading.current_thread().name == "register":
            register_loaded.set()
            assert allow_register.wait(timeout=5)
        return snapshot

    def save(value):
        value = copy.deepcopy(value)
        value["state_rev"] = int(value.get("state_rev") or 0) + 1
        stored.clear()
        stored.update(value)
        return copy.deepcopy(stored)

    original_link = os.link

    def observed_link(source, destination):
        result = original_link(source, destination)
        if destination.endswith("dev-repo.zip"):
            stage_activated.set()
        return result

    monkeypatch.setattr(bridge, "load_state", load)
    monkeypatch.setattr(bridge, "save_state", save)
    monkeypatch.setattr(bridge.os, "link", observed_link)
    monkeypatch.setattr(bridge, "STATE_MUTATION_LOCK", ObservedStateLock())
    register, register_result = _register_handler(bridge)
    stage, stage_result = _stage_handler(bridge, b"staged during registration")
    register_thread = threading.Thread(target=register.do_POST, name="register")
    stage_thread = threading.Thread(target=stage.do_POST, name="stage")

    register_thread.start()
    assert register_loaded.wait(timeout=5)
    stage_thread.start()
    assert stage_activated.wait(timeout=5)
    assert stage_waiting_for_state.wait(timeout=5)
    allow_register.set()
    register_thread.join(timeout=5)
    stage_thread.join(timeout=5)

    assert not register_thread.is_alive()
    assert not stage_thread.is_alive()
    assert register_result["payload"]["ok"] is True
    assert stage_result["payload"]["ok"] is True
    assert "registration" in stored
    assert stored["repo_zip"]["sha256"] == hashlib.sha256(
        b"staged during registration"
    ).hexdigest()
    assert (repo_dir / "dev-repo.zip").read_bytes() == b"staged during registration"


@pytest.mark.parametrize("failure", ["zero", "raise"])
def test_stage_descriptor_write_failure_and_next_request_succeeds(
    tmp_path, monkeypatch, failure
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    stored = _state_store(bridge, monkeypatch)
    original_write = os.write
    failed = False

    def fail_once(descriptor, body):
        nonlocal failed
        if not failed:
            failed = True
            if failure == "zero":
                return 0
            raise OSError("descriptor write failed")
        return original_write(descriptor, body)

    monkeypatch.setattr(bridge.os, "write", fail_once)
    first, first_result = _stage_handler(bridge, b"must not activate")

    first.do_POST()

    assert first_result["payload"]["error_code"] == "UPLOAD_FAILED"
    assert not (repo_dir / "dev-repo.zip").exists()
    assert stored == {"schema_version": bridge.STATE_SCHEMA_VERSION, "state_rev": 0}

    second_body = b"valid subsequent upload"
    second, second_result = _stage_handler(bridge, second_body)
    second.do_POST()

    assert second_result["payload"]["ok"] is True
    assert (repo_dir / "dev-repo.zip").read_bytes() == second_body
    assert stored["repo_zip"]["sha256"] == hashlib.sha256(second_body).hexdigest()
    assert list(repo_dir.glob("*.tmp*")) == []


def test_save_state_zero_descriptor_write_preserves_previous_state(tmp_path, monkeypatch):
    bridge = _bridge_module()
    _configure_local_vfs(bridge, tmp_path, monkeypatch)
    state_path = tmp_path / "state.json"
    previous = b'{"schema_version": 1, "state_rev": 4, "kept": true}'
    state_path.write_bytes(previous)
    monkeypatch.setattr(bridge.os, "write", lambda descriptor, body: 0)

    with pytest.raises(OSError):
        bridge.save_state({"new": True})

    assert state_path.read_bytes() == previous
    assert list(tmp_path.glob("state.json.tmp*")) == []


def test_capability_is_false_when_runtime_link_proof_is_unsupported(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    _configure_local_vfs(bridge, tmp_path, monkeypatch)
    handler = object.__new__(bridge.KodiBridgeHandler)
    handler.path = "/capabilities"
    captured = {}

    class Addon:
        def getAddonInfo(self, key):
            return "service.kodi_mcp"

    def unsupported_link(source, destination):
        raise OSError(errno.EPERM, "hard links unsupported")

    monkeypatch.setattr(bridge.xbmcaddon, "Addon", Addon)
    monkeypatch.setattr(bridge.os, "link", unsupported_link)
    monkeypatch.setattr(bridge, "_ATOMIC_FAIL_IF_EXISTS_CACHE", None, raising=False)
    handler._write_json = lambda payload, status=200: captured.update(payload)

    handler.do_GET()

    assert captured["endpoints"]["repo_stage"]["atomic_fail_if_exists"] is False


def test_private_stage_temp_exclusive_creation_refuses_precreated_symlink(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    _state_store(bridge, monkeypatch)
    token = "fixed-private-temp-token"
    target = tmp_path / "must-not-be-followed"
    target.write_bytes(b"protected")
    repo_dir.mkdir(parents=True)
    temp_path = repo_dir / ("dev-repo.zip.%s.tmp" % token)
    temp_path.symlink_to(target)

    class FixedUuid:
        hex = token

    monkeypatch.setattr(bridge.uuid, "uuid4", lambda: FixedUuid())
    monkeypatch.setattr(
        bridge, "_ATOMIC_FAIL_IF_EXISTS_CACHE", bridge.ATOMIC_LINK_SUPPORTED
    )
    handler, captured = _stage_handler(bridge, b"attacker-controlled overwrite")

    handler.do_POST()

    assert captured["payload"]["error_code"] == "UPLOAD_FAILED"
    assert target.read_bytes() == b"protected"
    assert temp_path.is_symlink()
    assert not (repo_dir / "dev-repo.zip").exists()


def test_overwrite_zero_descriptor_write_preserves_existing_then_recovers(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    stored = _state_store(bridge, monkeypatch)
    old_body = b"existing overwrite bytes"
    final_path = repo_dir / "dev-repo.zip"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(old_body)
    stored["repo_zip"] = _repo_metadata(bridge, old_body)
    before = copy.deepcopy(stored)
    original_write = os.write
    failed = False

    def zero_once(descriptor, body):
        nonlocal failed
        if not failed:
            failed = True
            return 0
        return original_write(descriptor, body)

    monkeypatch.setattr(bridge.os, "write", zero_once)
    first, first_result = _stage_handler(bridge, b"must not replace", mode="overwrite")
    first.do_POST()

    replacement = b"valid overwrite"
    second, second_result = _stage_handler(bridge, replacement, mode="overwrite")
    second.do_POST()

    assert first_result["payload"]["error_code"] == "UPLOAD_FAILED"
    assert second_result["payload"]["ok"] is True
    assert before["repo_zip"]["sha256"] == hashlib.sha256(old_body).hexdigest()
    assert final_path.read_bytes() == replacement
    assert stored["repo_zip"]["sha256"] == hashlib.sha256(replacement).hexdigest()
    assert list(repo_dir.glob("*.tmp*")) == []


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_stage_state_persistence_failure_keeps_zip_and_prior_state(
    tmp_path, monkeypatch, failure
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    state_path = tmp_path / "state.json"
    previous_state = {
        "schema_version": bridge.STATE_SCHEMA_VERSION,
        "state_rev": 7,
        "registration": {"server_id": "kept"},
    }
    previous_bytes = json.dumps(previous_state).encode("utf-8")
    state_path.write_bytes(previous_bytes)

    if failure == "write":
        original_write_owned = bridge._write_owned_temp

        def fail_state_write(owned, body):
            if "state.json.tmp." in owned.path:
                raise OSError("state write failed")
            return original_write_owned(owned, body)

        monkeypatch.setattr(bridge, "_write_owned_temp", fail_state_write)
    else:
        monkeypatch.setattr(
            bridge.os,
            "replace",
            lambda source, destination: (_ for _ in ()).throw(
                OSError("state replace failed")
            ),
        )

    body = ("stage after state %s failure" % failure).encode("utf-8")
    handler, captured = _stage_handler(bridge, body)
    handler.do_POST()

    status_handler = object.__new__(bridge.KodiBridgeHandler)
    status, status_code = status_handler._staged_repository_status()

    assert captured["payload"]["error_code"] == "STAGE_FAILED"
    assert (repo_dir / "dev-repo.zip").read_bytes() == body
    assert state_path.read_bytes() == previous_bytes
    assert list(tmp_path.glob("state.json.tmp.*")) == []
    assert status_code == 200
    assert status["exists"] is True
    assert status["sha256"] == hashlib.sha256(body).hexdigest()
    assert status["metadata_consistent"] is False


def test_registration_state_save_failure_is_truthful(monkeypatch):
    bridge = _bridge_module()
    register, captured = _register_handler(bridge)
    monkeypatch.setattr(
        bridge,
        "save_state",
        lambda state: (_ for _ in ()).throw(OSError("state save failed")),
    )
    monkeypatch.setattr(
        bridge,
        "load_state",
        lambda: {"schema_version": bridge.STATE_SCHEMA_VERSION, "state_rev": 0},
    )

    register.do_POST()

    assert captured == {
        "payload": {
            "ok": False,
            "error_code": "STATE_PERSIST_FAILED",
            "message": "registration state could not be persisted",
        },
        "status": 500,
    }


def test_runtime_atomic_link_probe_supported_and_cleans_files(tmp_path, monkeypatch):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)

    result = bridge._probe_atomic_fail_if_exists()

    assert result == bridge.ATOMIC_LINK_SUPPORTED
    assert list(repo_dir.glob(".atomic-link-probe-*")) == []


def test_runtime_atomic_link_probe_unsupported_and_cleans_files(tmp_path, monkeypatch):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    monkeypatch.setattr(
        bridge.os,
        "link",
        lambda source, destination: (_ for _ in ()).throw(
            OSError(errno.EOPNOTSUPP, "unsupported")
        ),
    )

    result = bridge._probe_atomic_fail_if_exists()

    assert result == bridge.ATOMIC_LINK_UNSUPPORTED
    assert list(repo_dir.glob(".atomic-link-probe-*")) == []


def test_runtime_atomic_link_probe_transient_error_is_not_supported(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    monkeypatch.setattr(
        bridge.os,
        "link",
        lambda source, destination: (_ for _ in ()).throw(OSError(errno.EIO, "io")),
    )

    result = bridge._probe_atomic_fail_if_exists()

    assert result == bridge.ATOMIC_LINK_ERROR
    assert list(repo_dir.glob(".atomic-link-probe-*")) == []


def test_capability_probe_initializes_once_across_concurrent_requests(monkeypatch):
    bridge = _bridge_module()
    monkeypatch.setattr(bridge, "_ATOMIC_FAIL_IF_EXISTS_CACHE", None)
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def probe():
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=5)
        return bridge.ATOMIC_LINK_SUPPORTED

    monkeypatch.setattr(bridge, "_probe_atomic_fail_if_exists", probe)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(bridge._atomic_fail_if_exists_status()))
        for _ in range(4)
    ]
    threads[0].start()
    assert started.wait(timeout=5)
    for thread in threads[1:]:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert calls == 1
    assert results == [bridge.ATOMIC_LINK_SUPPORTED] * 4


def test_transient_probe_error_advertises_atomic_conditional_false(monkeypatch):
    bridge = _bridge_module()
    handler, captured = _capabilities_handler(bridge, monkeypatch)
    monkeypatch.setattr(
        bridge,
        "_atomic_fail_if_exists_status",
        lambda: bridge.ATOMIC_LINK_ERROR,
    )

    handler.do_GET()

    assert captured["endpoints"]["repo_stage"] == {
        "method": "POST",
        "path": "/repo/stage",
        "auth_required": True,
        "auth_header": bridge.AUTH_HEADER_TOKEN,
        "modes": ["overwrite", "fail_if_exists"],
        "atomic_fail_if_exists": False,
    }


def test_unsupported_atomic_capability_blocks_conditional_before_body(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    handler, captured = _stage_handler(bridge, b"must not be read")
    bridge._ATOMIC_FAIL_IF_EXISTS_CACHE = bridge.ATOMIC_LINK_UNSUPPORTED

    class UnreadableBody:
        def read(self, size=-1):
            raise AssertionError("unsupported conditional body was read")

    handler.rfile = UnreadableBody()
    handler.do_POST()

    assert captured == {
        "payload": {
            "ok": False,
            "error_code": "ATOMIC_FAIL_IF_EXISTS_UNSUPPORTED",
            "message": "atomic conditional staging is unavailable",
        },
        "status": 503,
    }
    assert not repo_dir.exists()


def test_overwrite_still_works_when_atomic_conditional_is_unsupported(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    stored = _state_store(bridge, monkeypatch)
    body = b"overwrite without hard links"
    handler, captured = _stage_handler(bridge, body, mode="overwrite")
    bridge._ATOMIC_FAIL_IF_EXISTS_CACHE = bridge.ATOMIC_LINK_UNSUPPORTED
    monkeypatch.setattr(
        bridge,
        "_probe_atomic_fail_if_exists",
        lambda: (_ for _ in ()).throw(AssertionError("overwrite probed")),
    )

    handler.do_POST()

    assert captured["payload"]["ok"] is True
    assert (repo_dir / "dev-repo.zip").read_bytes() == body
    assert stored["repo_zip"]["sha256"] == hashlib.sha256(body).hexdigest()


def test_slot_and_state_lock_order_has_no_reverse_acquisition():
    bridge = _bridge_module()
    tree = ast.parse(inspect.getsource(bridge))
    reverse = []
    slot_then_state = []

    def visit(node, held=()):
        next_held = held
        if isinstance(node, ast.With):
            names = {
                item.context_expr.id
                for item in node.items
                if isinstance(item.context_expr, ast.Name)
            }
            if "REPO_STAGE_SLOT_LOCK" in names and "STATE_MUTATION_LOCK" in held:
                reverse.append(node.lineno)
            if "STATE_MUTATION_LOCK" in names and "REPO_STAGE_SLOT_LOCK" in held:
                slot_then_state.append(node.lineno)
            next_held = held + tuple(sorted(names))
        for child in ast.iter_child_nodes(node):
            visit(child, next_held)

    visit(tree)

    assert reverse == []
    assert slot_then_state


def test_state_update_uses_local_replace_not_windows_vfs_rename(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    _configure_local_vfs(bridge, tmp_path, monkeypatch)
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({"schema_version": 1, "state_rev": 3, "old": True}),
        encoding="utf-8",
    )
    rename_calls = []
    monkeypatch.setattr(
        bridge.xbmcvfs,
        "rename",
        lambda source, destination: rename_calls.append((source, destination)) or False,
    )

    saved = bridge.save_state({"schema_version": 1, "state_rev": 3, "new": True})

    assert saved["new"] is True
    assert bridge.load_state()["new"] is True
    assert rename_calls == []


def test_stage_write_keeps_owned_descriptor_when_temp_path_is_substituted(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    _state_store(bridge, monkeypatch)
    target = tmp_path / "symlink-target"
    target.write_bytes(b"protected")
    original_create = bridge._create_owned_temp
    substituted = []

    def create_then_substitute(path):
        owned = original_create(path)
        os.unlink(path)
        os.symlink(target, path)
        substituted.append(path)
        return owned

    monkeypatch.setattr(bridge, "_create_owned_temp", create_then_substitute)
    handler, captured = _stage_handler(bridge, b"must stay on owned inode")

    handler.do_POST()

    assert captured["payload"]["error_code"] == "STAGE_FAILED"
    assert target.read_bytes() == b"protected"
    assert os.path.islink(substituted[0])
    assert not (repo_dir / "dev-repo.zip").exists()


def test_post_write_stage_substitution_is_rejected_without_deleting_replacement(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    _state_store(bridge, monkeypatch)
    replacement_target = tmp_path / "replacement-target"
    replacement_target.write_bytes(b"replacement stays")
    original_lexists = bridge.os.path.lexists
    substituted = []

    def substitute_before_activation(path):
        if path.endswith("dev-repo.zip"):
            temps = list(repo_dir.glob("dev-repo.zip.*.tmp"))
            if temps and not substituted:
                os.unlink(temps[0])
                os.symlink(replacement_target, temps[0])
                substituted.append(str(temps[0]))
        return original_lexists(path)

    monkeypatch.setattr(bridge.os.path, "lexists", substitute_before_activation)
    handler, captured = _stage_handler(bridge, b"fully written owned bytes")

    handler.do_POST()

    assert captured["payload"]["error_code"] == "STAGE_FAILED"
    assert replacement_target.read_bytes() == b"replacement stays"
    assert os.path.islink(substituted[0])
    assert not (repo_dir / "dev-repo.zip").exists()


def test_probe_cleanup_failure_cannot_return_supported(tmp_path, monkeypatch):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    original_unlink = os.unlink

    def fail_probe_destination_cleanup(path):
        if ".atomic-link-probe-" in str(path) and str(path).endswith(".dst"):
            raise PermissionError("cleanup denied")
        return original_unlink(path)

    monkeypatch.setattr(bridge.os, "unlink", fail_probe_destination_cleanup)
    result = bridge._probe_atomic_fail_if_exists()
    leftovers = list(repo_dir.glob(".atomic-link-probe-*"))
    for path in leftovers:
        original_unlink(path)

    assert result == bridge.ATOMIC_LINK_ERROR
    assert leftovers


def test_precreated_state_temp_collision_is_not_deleted(tmp_path, monkeypatch):
    bridge = _bridge_module()
    _configure_local_vfs(bridge, tmp_path, monkeypatch)
    token = "state-collision-token"
    state_path = tmp_path / "state.json"
    state_path.write_bytes(b'{"schema_version": 1, "state_rev": 2}')
    collision_target = tmp_path / "state-collision-target"
    collision_target.write_bytes(b"protected collision")
    collision = tmp_path / ("state.json.tmp.%s" % token)
    collision.symlink_to(collision_target)

    class FixedUuid:
        hex = token

    monkeypatch.setattr(bridge.uuid, "uuid4", lambda: FixedUuid())

    with pytest.raises(OSError):
        bridge.save_state({"new": True})

    assert collision.is_symlink()
    assert collision_target.read_bytes() == b"protected collision"
    assert bridge.load_state()["state_rev"] == 2


def test_state_temp_substitution_is_rejected_without_touching_attacker(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    _configure_local_vfs(bridge, tmp_path, monkeypatch)
    state_path = tmp_path / "state.json"
    previous = b'{"schema_version": 1, "state_rev": 5, "kept": true}'
    state_path.write_bytes(previous)
    attacker_target = tmp_path / "state-attacker-target"
    attacker_target.write_bytes(b"attacker bytes")
    original_create = bridge._create_owned_temp
    substituted = []

    def create_then_substitute(path):
        owned = original_create(path)
        if "state.json.tmp." in path:
            os.unlink(path)
            os.symlink(attacker_target, path)
            substituted.append(path)
        return owned

    monkeypatch.setattr(bridge, "_create_owned_temp", create_then_substitute)

    with pytest.raises(OSError):
        bridge.save_state({"new": True})

    assert state_path.read_bytes() == previous
    assert attacker_target.read_bytes() == b"attacker bytes"
    assert os.path.islink(substituted[0])


def test_probe_destination_collision_is_not_deleted(tmp_path, monkeypatch):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    token = "probe-destination-collision"
    repo_dir.mkdir(parents=True)
    destination = repo_dir / (".atomic-link-probe-%s.dst" % token)
    destination.write_bytes(b"preexisting destination")
    before = destination.stat()

    class FixedUuid:
        hex = token

    monkeypatch.setattr(bridge.uuid, "uuid4", lambda: FixedUuid())

    result = bridge._probe_atomic_fail_if_exists()

    after = destination.stat()
    assert result == bridge.ATOMIC_LINK_ERROR
    assert destination.read_bytes() == b"preexisting destination"
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert not (repo_dir / (".atomic-link-probe-%s.src" % token)).exists()


def test_probe_source_collision_is_not_deleted(tmp_path, monkeypatch):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    token = "probe-source-collision"
    repo_dir.mkdir(parents=True)
    target = tmp_path / "probe-source-target"
    target.write_bytes(b"protected source")
    source = repo_dir / (".atomic-link-probe-%s.src" % token)
    source.symlink_to(target)

    class FixedUuid:
        hex = token

    monkeypatch.setattr(bridge.uuid, "uuid4", lambda: FixedUuid())

    result = bridge._probe_atomic_fail_if_exists()

    assert result == bridge.ATOMIC_LINK_ERROR
    assert source.is_symlink()
    assert target.read_bytes() == b"protected source"


def test_cleanup_failure_is_cached_false_and_blocks_conditional(
    tmp_path, monkeypatch
):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    original_unlink = os.unlink

    def fail_probe_destination_cleanup(path):
        if ".atomic-link-probe-" in str(path) and str(path).endswith(".dst"):
            raise PermissionError("cleanup denied")
        return original_unlink(path)

    monkeypatch.setattr(bridge.os, "unlink", fail_probe_destination_cleanup)
    monkeypatch.setattr(bridge, "_ATOMIC_FAIL_IF_EXISTS_CACHE", None)

    status = bridge._atomic_fail_if_exists_status()
    capabilities, advertised = _capabilities_handler(bridge, monkeypatch)
    capabilities.do_GET()
    conditional, refused = _stage_handler(bridge, b"must not be read")
    bridge._ATOMIC_FAIL_IF_EXISTS_CACHE = status

    class UnreadableBody:
        def read(self, size=-1):
            raise AssertionError("cleanup-failed capability read body")

    conditional.rfile = UnreadableBody()
    conditional.do_POST()
    leftovers = list(repo_dir.glob(".atomic-link-probe-*"))
    for path in leftovers:
        original_unlink(path)

    assert status == bridge.ATOMIC_LINK_ERROR
    assert bridge._ATOMIC_FAIL_IF_EXISTS_CACHE == bridge.ATOMIC_LINK_ERROR
    assert advertised["endpoints"]["repo_stage"]["atomic_fail_if_exists"] is False
    assert refused["status"] == 503
    assert refused["payload"]["error_code"] == "ATOMIC_FAIL_IF_EXISTS_UNSUPPORTED"


def test_partial_descriptor_writes_complete_exact_stage(tmp_path, monkeypatch):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    stored = _state_store(bridge, monkeypatch)
    original_write = os.write
    partial_calls = []

    def partial_write(descriptor, body):
        limited = body[: max(1, len(body) // 2)]
        partial_calls.append((len(body), len(limited)))
        return original_write(descriptor, limited)

    monkeypatch.setattr(bridge.os, "write", partial_write)
    body = b"partial descriptor writes must produce exact final bytes"
    handler, captured = _stage_handler(bridge, body)

    handler.do_POST()

    final_path = repo_dir / "dev-repo.zip"
    assert captured["payload"]["ok"] is True
    assert any(written < requested for requested, written in partial_calls)
    assert final_path.read_bytes() == body
    assert stored["repo_zip"]["size_bytes"] == len(body)
    assert stored["repo_zip"]["sha256"] == hashlib.sha256(body).hexdigest()


def test_overwrite_replace_failure_preserves_final_and_metadata(tmp_path, monkeypatch):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    stored = _state_store(bridge, monkeypatch)
    old_body = b"old final survives replace failure"
    final_path = repo_dir / "dev-repo.zip"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(old_body)
    stored["repo_zip"] = _repo_metadata(bridge, old_body)
    before = copy.deepcopy(stored)
    original_replace = os.replace

    def fail_stage_replace(source, destination):
        if destination == str(final_path):
            raise OSError("stage replace failed")
        return original_replace(source, destination)

    monkeypatch.setattr(bridge.os, "replace", fail_stage_replace)
    handler, captured = _stage_handler(bridge, b"replacement must not land", mode="overwrite")

    handler.do_POST()

    assert captured["payload"]["error_code"] == "STAGE_FAILED"
    assert final_path.read_bytes() == old_body
    assert stored == before
    assert list(repo_dir.glob("*.tmp")) == []


@pytest.mark.parametrize("mode", ["fail_if_exists", "overwrite"])
def test_successful_stage_final_is_regular_and_exact(tmp_path, monkeypatch, mode):
    bridge = _bridge_module()
    repo_dir = _configure_local_vfs(bridge, tmp_path, monkeypatch)
    stored = _state_store(bridge, monkeypatch)
    final_path = repo_dir / "dev-repo.zip"
    if mode == "overwrite":
        final_path.parent.mkdir(parents=True)
        final_path.write_bytes(b"old")
    body = ("regular final via %s" % mode).encode("utf-8")
    handler, captured = _stage_handler(bridge, body, mode=mode)

    handler.do_POST()

    final_stat = os.lstat(final_path)
    assert captured["payload"]["ok"] is True
    assert stat.S_ISREG(final_stat.st_mode)
    assert not final_path.is_symlink()
    assert final_path.read_bytes() == body
    assert stored["repo_zip"]["size_bytes"] == len(body)
    assert stored["repo_zip"]["sha256"] == hashlib.sha256(body).hexdigest()


def test_stage_hash_and_count_follow_confirmed_descriptor_write():
    bridge = _bridge_module()
    source = inspect.getsource(bridge.KodiBridgeHandler.do_POST)
    write_position = source.index("_write_owned_temp(owned, chunk)")
    hash_position = source.index("sha.update(chunk)", write_position)
    count_position = source.index("bytes_written += len(chunk)", hash_position)

    assert write_position < hash_position < count_position


@pytest.mark.parametrize("translated", ["special://profile/x", "smb://host/x", "relative/x"])
def test_bridge_owned_local_path_rejects_unresolved_or_nonlocal_translation(
    monkeypatch, translated
):
    bridge = _bridge_module()
    monkeypatch.setattr(bridge, "_translate", lambda special_path: translated)

    with pytest.raises(OSError):
        bridge._translated_local_path(bridge.STATE_SPECIAL_PATH)
