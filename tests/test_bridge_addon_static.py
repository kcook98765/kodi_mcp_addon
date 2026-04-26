import ast
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


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
        setting_ids = {
            node.attrib.get("id")
            for node in tree.getroot().iter("setting")
        }
        self.assertIn("mcp_token", setting_ids)

    def test_bridge_code_has_milestone_a_routes(self):
        source = (SERVICE_DIR / "http_bridge.py").read_text(encoding="utf-8")

        for route in ("/mcp/register", "/mcp/state", "/repo/stage"):
            self.assertIn(route, source)

        for helper in ("_register_mcp_server", "_mcp_state", "_repo_stage"):
            self.assertIn("def %s" % helper, source)

    def test_python_sources_parse(self):
        for path in (
            SERVICE_DIR / "http_bridge.py",
            SERVICE_DIR / "service.py",
            ROOT / "packages" / "script.kodi_mcp_test" / "default.py",
        ):
            with self.subTest(path=path):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


if __name__ == "__main__":
    unittest.main()
