import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "packages" / "service.kodi_mcp" / "http_bridge.py"


class _FakeAddon:
    settings = {"mcp_token": ""}

    def __init__(self, id=None):
        self.id = id or "service.kodi_mcp"

    def getSetting(self, key):
        return self.settings.get(key, "")

    def getAddonInfo(self, key):
        values = {
            "id": self.id,
            "version": "0.2.17",
            "path": "/tmp/fake-kodi/addons/service.kodi_mcp",
            "profile": "/tmp/fake-kodi/profile/addon_data/service.kodi_mcp",
        }
        return values.get(key, "")


class _FakeFile:
    def __init__(self, path, mode="r"):
        self.path = Path(path)
        self.mode = mode
        if "w" in mode:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open(mode)

    def read(self, *args):
        return self.handle.read(*args)

    def write(self, body):
        return self.handle.write(body)

    def close(self):
        self.handle.close()


class _FakeXbmcvfs(types.SimpleNamespace):
    def __init__(self, root):
        super().__init__()
        self.root = Path(root)

    def translatePath(self, path):
        path = str(path)
        replacements = {
            "special://profile": self.root / "profile",
            "special://logpath": self.root / "logs",
        }
        for prefix, target in replacements.items():
            if path.startswith(prefix):
                suffix = path[len(prefix):].lstrip("/")
                return str(target / suffix)
        return path

    def exists(self, path):
        return Path(path).exists()

    def mkdirs(self, path):
        Path(path).mkdir(parents=True, exist_ok=True)

    def File(self, path, mode="r"):
        return _FakeFile(path, mode)


def _load_bridge_module(tmp_path):
    calls = []

    fake_xbmc = types.SimpleNamespace(
        LOGDEBUG=0,
        LOGINFO=1,
        log=lambda *args, **kwargs: None,
        getCondVisibility=lambda *args, **kwargs: False,
        executebuiltin=lambda *args, **kwargs: calls.append(("builtin", args, kwargs)),
        executeJSONRPC=lambda payload: '{"jsonrpc":"2.0","id":"test","result":"OK"}',
    )
    fake_xbmc.calls = calls
    fake_xbmcaddon = types.SimpleNamespace(Addon=_FakeAddon)
    fake_xbmcvfs = _FakeXbmcvfs(tmp_path)

    sys.modules["xbmc"] = fake_xbmc
    sys.modules["xbmcaddon"] = fake_xbmcaddon
    sys.modules["xbmcvfs"] = fake_xbmcvfs

    module_name = "bridge_under_test_%s" % id(tmp_path)
    spec = importlib.util.spec_from_file_location(module_name, BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BridgeHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        _FakeAddon.settings = {"mcp_token": ""}
        self.bridge = _load_bridge_module(self.tmp_path)
        self.handler = object.__new__(self.bridge.KodiBridgeHandler)
        self.handler.headers = {}

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_token_auth_when_configured(self):
        _FakeAddon.settings = {"mcp_token": "secret"}

        self.handler.headers = {}
        self.assertFalse(self.handler._authorize())

        self.handler.headers = {"X-Kodi-MCP-Token": "secret"}
        self.assertTrue(self.handler._authorize())

    def test_public_get_paths_do_not_require_token(self):
        public_paths = {"/health", "/status", "/runtime/info", "/capabilities", "/control/capabilities"}

        for path in public_paths:
            self.assertTrue(self.handler._is_public_get_path(path))

        self.assertFalse(self.handler._is_public_get_path("/gui/screenshot"))
        self.assertFalse(self.handler._is_public_get_path("/log/tail"))

    def test_protected_get_paths_reject_missing_token(self):
        _FakeAddon.settings = {"mcp_token": "secret"}
        writes = []

        self.handler.path = "/gui/screenshot"
        self.handler.headers = {}
        self.handler.send_response = lambda status: writes.append(("status", status))
        self.handler.send_header = lambda name, value: writes.append(("header", name, value))
        self.handler.end_headers = lambda: writes.append(("end",))
        self.handler.wfile = io.BytesIO()

        self.handler.do_GET()

        self.assertIn(("status", 401), writes)
        self.assertEqual(json.loads(self.handler.wfile.getvalue().decode("utf-8")), {"error": "unauthorized"})

    def test_protected_post_paths_reject_missing_token(self):
        _FakeAddon.settings = {"mcp_token": "secret"}
        writes = []

        self.handler.path = "/gui/action"
        self.handler.headers = {"Content-Length": "0"}
        self.handler.rfile = io.BytesIO(b"")
        self.handler.send_response = lambda status: writes.append(("status", status))
        self.handler.send_header = lambda name, value: writes.append(("header", name, value))
        self.handler.end_headers = lambda: writes.append(("end",))
        self.handler.wfile = io.BytesIO()

        self.handler.do_POST()

        self.assertIn(("status", 401), writes)
        self.assertEqual(json.loads(self.handler.wfile.getvalue().decode("utf-8")), {"error": "unauthorized"})

    def test_repo_stage_rejects_sha_mismatch(self):
        self.handler.headers = {"X-Content-SHA256": "0" * 64}

        envelope, status = self.handler._repo_stage("dev-repo", "overwrite", b"PK\x03\x04data")

        self.assertEqual(status, 400)
        self.assertFalse(envelope["result"]["ok"])
        self.assertEqual(envelope["result"]["error"], "sha256 mismatch")

    def test_register_stage_and_state_enable_dev_setup(self):
        body = b"PK\x03\x04data"
        stage_envelope, stage_status = self.handler._repo_stage("dev-repo", "overwrite", body)
        self.assertEqual(stage_status, 200)
        self.assertTrue(Path(stage_envelope["result"]["saved_path"]).exists())

        register_envelope, register_status = self.handler._register_mcp_server(
            {"server_id": "kodi-mcp", "ttl_seconds": 60}
        )
        self.assertEqual(register_status, 200)
        self.assertEqual(register_envelope["result"]["registration"]["server_id"], "kodi-mcp")

        state_envelope, state_status = self.handler._mcp_state()
        self.assertEqual(state_status, 200)
        self.assertTrue(state_envelope["result"]["derived"]["dev_setup_available"])
        self.assertTrue(state_envelope["result"]["install_hint"]["path"].endswith("dev-repo.zip"))
        self.assertEqual(state_envelope["result"]["install_hint"]["action"], "Kodi UI: Add-ons > Install from repository")
        self.assertEqual(state_envelope["result"]["install_hint"]["repository_addon_id"], "repository.kodi-mcp")
        self.assertIn("not an installable Kodi add-on zip", state_envelope["result"]["install_hint"]["note"])

    def test_repo_stage_state_rehydrates_existing_zip(self):
        body = b"PK\x03\x04data"
        stage_envelope, _ = self.handler._repo_stage("dev-repo", "overwrite", body)
        saved_path = stage_envelope["result"]["saved_path"]

        self.bridge.MCP_STATE["repo_zip"] = None
        state_envelope, status = self.handler._mcp_state()

        self.assertEqual(status, 200)
        repo_zip = state_envelope["result"]["state"]["repo_zip"]
        self.assertEqual(repo_zip["saved_path"], saved_path)
        self.assertTrue(repo_zip["rehydrated"])

    def test_capabilities_report_milestone_a_features(self):
        capabilities = self.handler._get_capabilities()

        self.assertIn("/mcp/register", capabilities["endpoints"])
        self.assertIn("/repo/stage", capabilities["endpoints"])
        self.assertIn("/gui/action", capabilities["endpoints"])
        self.assertIn("/gui/screenshot", capabilities["endpoints"])
        self.assertTrue(capabilities["features"]["repo_zip_staging"])
        self.assertTrue(capabilities["features"]["screenshots"])

    def test_gui_action_uses_jsonrpc_input_method(self):
        result, status = self.handler._gui_action("down")

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "Input.Down")

    def test_gui_action_rejects_unknown_action(self):
        result, status = self.handler._gui_action("launch")

        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "unsupported gui action")
        self.assertIn("select", result["allowed"])

    def test_capture_screenshot_returns_pending_when_file_not_observed(self):
        result, status = self.handler._capture_screenshot(include_image=False)

        self.assertEqual(status, 202)
        self.assertFalse(result["ok"])
        self.assertTrue(result["path"].endswith(".png"))

    def test_capture_screenshot_returns_pending_for_empty_file(self):
        original_builtin = sys.modules["xbmc"].executebuiltin

        def _write_empty_screenshot(command, *args, **kwargs):
            path = command.split("TakeScreenshot(", 1)[1].split(",", 1)[0]
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"")

        sys.modules["xbmc"].executebuiltin = _write_empty_screenshot
        try:
            result, status = self.handler._capture_screenshot(include_image=False)
        finally:
            sys.modules["xbmc"].executebuiltin = original_builtin

        self.assertEqual(status, 202)
        self.assertEqual(result["error"], "screenshot file was created but remained empty")


if __name__ == "__main__":
    unittest.main()
