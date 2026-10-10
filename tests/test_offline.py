"""Offline tests: MCP protocol behavior, tool schemas, geometry conversion. No network."""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from arcgis_online_mcp import geometry  # noqa: E402
from arcgis_online_mcp.client import ArcGISClient  # noqa: E402
from arcgis_online_mcp.server import Server  # noqa: E402
from arcgis_online_mcp.tools import TOOLS, available_tools  # noqa: E402

CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("ARCGIS_")}


class StdioProtocolTest(unittest.TestCase):
    """Drive the real server.py over stdio, the way an MCP client does."""

    def setUp(self):
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=CLEAN_ENV,
        )
        self.next_id = 0

    def tearDown(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.proc.stderr.close()

    def send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def rpc(self, method, params=None):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        reply = json.loads(self.proc.stdout.readline())
        self.assertEqual(reply["id"], self.next_id)
        return reply

    def test_handshake_and_listing(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "0"}})["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["capabilities"])
        self.assertEqual(init["serverInfo"]["name"], "arcgis-online-mcp")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})  # must not produce output
        self.assertEqual(self.rpc("ping")["result"], {})
        names = {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertIn("query_features", names)
        self.assertNotIn("apply_edits", names, "write tools must be hidden unless ARCGIS_ALLOW_WRITES=true")
        self.assertNotIn("sign_in", names, "sign_in only appears in OAuth mode")

    def test_unknown_protocol_version_falls_back(self):
        init = self.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertRegex(init["protocolVersion"], r"^\d{4}-\d{2}-\d{2}$")

    def test_errors(self):
        self.rpc("initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(self.rpc("nope/nope")["error"]["code"], -32601)
        self.assertEqual(self.rpc("tools/call", {"name": "nope", "arguments": {}})["error"]["code"], -32602)
        res = self.rpc("tools/call", {"name": "get_item", "arguments": {}})["result"]
        self.assertTrue(res["isError"])
        self.assertIn("item_id", res["content"][0]["text"])
        res = self.rpc("tools/call", {"name": "get_item", "arguments": {"item_id": "x", "bogus": 1}})["result"]
        self.assertTrue(res["isError"])
        self.assertIn("bogus", res["content"][0]["text"])
        self.proc.stdin.write("not json\n")
        self.proc.stdin.flush()
        self.assertEqual(json.loads(self.proc.stdout.readline())["error"]["code"], -32700)


class ToolSchemaTest(unittest.TestCase):
    def test_schemas_are_well_formed(self):
        names = set()
        for t in TOOLS:
            spec = t.spec()
            self.assertNotIn(t.name, names)
            names.add(t.name)
            self.assertRegex(t.name, r"^[a-z][a-z0-9_]{0,63}$")
            self.assertGreater(len(spec["description"]), 20, t.name)
            self.assertEqual(spec["inputSchema"]["type"], "object")
            for req in spec["inputSchema"].get("required", []):
                self.assertIn(req, spec["inputSchema"]["properties"], t.name)
            self.assertEqual(spec["annotations"]["readOnlyHint"], not t.write)
            json.dumps(spec)

    def test_write_gate(self):
        old = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(CLEAN_ENV)
            self.assertFalse(any(t.write for t in available_tools(ArcGISClient())))
            os.environ["ARCGIS_ALLOW_WRITES"] = "true"
            writes = {t.name for t in available_tools(ArcGISClient()) if t.write}
            self.assertEqual(writes, {"apply_edits", "create_hosted_layer", "create_webmap", "update_item", "share_item"})
            os.environ["ARCGIS_ALLOW_WRITES"] = "${user_config.allow_writes}"  # unsubstituted template
            self.assertFalse(ArcGISClient().allow_writes)
        finally:
            os.environ.clear()
            os.environ.update(old)


class TokenScopeTest(unittest.TestCase):
    def test_token_only_sent_to_trusted_hosts(self):
        old = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(CLEAN_ENV)
            os.environ["ARCGIS_API_KEY"] = "test-key"
            c = ArcGISClient()
            self.assertTrue(c._token_allowed_for("https://services3.arcgis.com/abc/arcgis/rest/services/x/FeatureServer/0"))
            self.assertTrue(c._token_allowed_for("https://www.arcgis.com/sharing/rest/search"))
            self.assertFalse(c._token_allowed_for("http://services.arcgis.com/x"), "never over plain http")
            self.assertFalse(c._token_allowed_for("https://evil.example.com/arcgis/rest/services/x"))
            self.assertFalse(c._token_allowed_for("https://arcgis.com.evil.example/x"))
            os.environ["ARCGIS_TOKEN_HOSTS"] = "gis.example.gov"
            self.assertTrue(ArcGISClient()._token_allowed_for("https://gis.example.gov/server/rest/services"))
        finally:
            os.environ.clear()
            os.environ.update(old)


class GeometryTest(unittest.TestCase):
    def test_point_roundtrip(self):
        g = geometry.esri_to_geojson({"x": -87.6, "y": 41.9})
        self.assertEqual(g, {"type": "Point", "coordinates": [-87.6, 41.9]})
        e = geometry.geojson_to_esri(g, 4326)
        self.assertEqual((e["x"], e["y"], e["spatialReference"]["wkid"]), (-87.6, 41.9, 4326))

    def test_web_mercator(self):
        e = geometry.geojson_to_esri({"type": "Point", "coordinates": [-87.6298, 41.8781]}, 102100)
        self.assertAlmostEqual(e["x"], -9754904.12, delta=1)
        self.assertAlmostEqual(e["y"], 5142736.87, delta=1)
        self.assertEqual(e["spatialReference"]["latestWkid"], 3857)

    def test_polygon_winding(self):
        ccw = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]  # GeoJSON exterior (counter-clockwise)
        hole_cw = [[0.2, 0.2], [0.2, 0.8], [0.8, 0.8], [0.8, 0.2], [0.2, 0.2]]
        esri = geometry.geojson_to_esri({"type": "Polygon", "coordinates": [ccw, hole_cw]})
        self.assertLess(geometry._ring_area(esri["rings"][0]), 0, "Esri exterior must be clockwise")
        self.assertGreater(geometry._ring_area(esri["rings"][1]), 0, "Esri hole must be counter-clockwise")
        back = geometry.esri_to_geojson(esri)
        self.assertEqual(back["type"], "Polygon")
        self.assertEqual(len(back["coordinates"]), 2)
        self.assertGreater(geometry._ring_area(back["coordinates"][0]), 0)

    def test_multipolygon(self):
        a = [[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]
        b = [[5, 5], [5, 6], [6, 6], [6, 5], [5, 5]]
        g = geometry.esri_to_geojson({"rings": [a, b]})
        self.assertEqual(g["type"], "MultiPolygon")
        self.assertEqual(len(g["coordinates"]), 2)

    def test_lines(self):
        self.assertEqual(geometry.esri_to_geojson({"paths": [[[0, 0], [1, 1]]]})["type"], "LineString")
        self.assertEqual(geometry.esri_to_geojson({"paths": [[[0, 0], [1, 1]], [[2, 2], [3, 3]]]})["type"], "MultiLineString")
        self.assertIsNone(geometry.esri_to_geojson(None))


class ServerInProcessTest(unittest.TestCase):
    def test_truncates_huge_results(self):
        server = Server(client=ArcGISClient())
        from arcgis_online_mcp import tools

        big = tools.Tool("big_test", "returns a lot of text for testing", {"type": "object", "properties": {}},
                         lambda c, a: "x" * 400_000)
        tools.TOOLS.append(big)
        try:
            res = server.call_tool("big_test", {})
            self.assertLess(len(res["content"][0]["text"]), 151_000)
            self.assertIn("truncated", res["content"][0]["text"])
        finally:
            tools.TOOLS.remove(big)


if __name__ == "__main__":
    unittest.main()
