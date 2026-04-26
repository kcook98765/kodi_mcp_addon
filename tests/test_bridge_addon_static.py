import ast
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "packages" / "service.kodi_mcp"


class BridgeAddonStaticTests(unittest.TestCase):
    def test_service_manifest_metadata(self):
        tree = ET.parse(SERVICE_DIR / "addon.xml")
        addon = tree.getroot()

        self.assertEqual(addon.attrib["id"], "service.kodi_mcp")
        self.assertEqual(addon.attrib["version"], "0.2.16")

        service_extensions = [
            ext
            for ext in addon.findall("extension")
            if ext.attrib.get("point") == "xbmc.service"
        ]
        self.assertEqual(len(service_extensions), 1)
        self.assertEqual(service_extensions[0].attrib["library"], "service.py")

    def test_settings_define_mcp_token(self):
        tree = ET.parse(SERVICE_DIR / "resources" / "settings.xml")
        settings = {
            node.attrib.get("id"): node
            for node in tree.getroot().iter("setting")
        }
        self.assertIn("mcp_token", settings)
        control = settings["mcp_token"].find("control")
        default = settings["mcp_token"].find("default")
        constraints = settings["mcp_token"].find("constraints")
        level = settings["mcp_token"].find("level")
        self.assertIsNotNone(default)
        self.assertIsNotNone(control)
        self.assertIsNotNone(constraints)
        self.assertEqual(level.text, "0")
        self.assertEqual(control.attrib["type"], "edit")
        self.assertEqual(control.attrib["format"], "string")
        self.assertEqual(constraints.find("allowempty").text, "true")

    def test_bridge_code_has_milestone_a_routes(self):
        source = (SERVICE_DIR / "http_bridge.py").read_text(encoding="utf-8")

        for route in ("/mcp/register", "/mcp/state", "/repo/stage", "/gui/action", "/gui/screenshot"):
            self.assertIn(route, source)

        for helper in ("_register_mcp_server", "_mcp_state", "_repo_stage"):
            self.assertIn("def %s" % helper, source)

    def test_python_sources_parse(self):
        for path in (
            SERVICE_DIR / "http_bridge.py",
            SERVICE_DIR / "service.py",
            ROOT / "packages" / "script.kodi_mcp_test" / "default.py",
            ROOT / "scripts" / "build_service_addon.py",
        ):
            with self.subTest(path=path):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_build_service_addon_zip_contains_addon_root(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_service_addon.py"),
                    "--output-dir",
                    tmp_dir,
                ],
                check=True,
                cwd=ROOT,
                text=True,
                capture_output=True,
            )

            zip_path = Path(tmp_dir) / "service.kodi_mcp-0.2.16.zip"
            self.assertTrue(zip_path.exists())
            with ZipFile(zip_path) as zf:
                names = set(zf.namelist())
            self.assertIn("service.kodi_mcp/addon.xml", names)
            self.assertIn("service.kodi_mcp/http_bridge.py", names)
            self.assertIn("service.kodi_mcp/resources/settings.xml", names)


if __name__ == "__main__":
    unittest.main()
