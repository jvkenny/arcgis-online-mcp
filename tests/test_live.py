"""Live tests against public ArcGIS Online content (Esri Living Atlas). Anonymous; no account needed.

Skipped when ARCGIS_SKIP_LIVE=1. These hit the real service, so they catch API drift.
"""

import csv
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from arcgis_online_mcp.client import ArcGISClient  # noqa: E402
from arcgis_online_mcp.server import Server  # noqa: E402

STATES_ITEM = "8c2d6d7df8fa4142b0a1211c8dd66903"  # USA States Generalized Boundaries (esri_dm)
TRACTS_ITEM = "20f5d275113e4066bf311236d9dcc3d4"  # USA Census Tract Boundaries (esri_dm)


@unittest.skipIf(os.environ.get("ARCGIS_SKIP_LIVE") == "1", "live tests disabled")
class LiveAnonymousTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update({k: v for k, v in saved.items() if not k.startswith("ARCGIS_")})
        try:
            cls.server = Server(client=ArcGISClient())
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def call(self, name, **args):
        res = self.server.call_tool(name, args)
        text = res["content"][0]["text"]
        self.assertFalse(res["isError"], f"{name} failed: {text}")
        return json.loads(text)

    def call_error(self, name, **args):
        res = self.server.call_tool(name, args)
        self.assertTrue(res["isError"])
        return res["content"][0]["text"]

    def test_search_and_item(self):
        found = self.call("search_content", q="USA States Generalized Boundaries", owner="esri_dm",
                          item_type="Feature Service", num=5)
        self.assertIn(STATES_ITEM, [i["id"] for i in found["items"]])
        item = self.call("get_item", item_id=STATES_ITEM)
        self.assertEqual(item["type"], "Feature Service")
        self.assertEqual(item["layers"][0]["geometry_type"], "esriGeometryPolygon")
        self.assertNotIn("<", item.get("description", ""), "HTML should be stripped")

    def test_describe_layer(self):
        layer = self.call("describe_layer", item_id=STATES_ITEM)
        names = [f["name"] for f in layer["fields"]]
        self.assertIn("STATE_NAME", names)
        self.assertGreaterEqual(layer["record_count"], 50)
        xmin, ymin, xmax, ymax = layer["extent_wgs84"]
        self.assertLess(xmin, -150)
        self.assertGreater(ymax, 60)

    def test_query_attributes_and_geometry(self):
        res = self.call("query_features", item_id=STATES_ITEM, where="STATE_ABBR = 'IL'",
                        out_fields=["STATE_NAME", "POPULATION"], return_geometry=True, geometry_precision=3)
        feat = res["geojson"]["features"][0]
        self.assertEqual(feat["properties"]["STATE_NAME"], "Illinois")
        self.assertIsNotNone(feat["id"], "object ID should be kept even when not requested")
        self.assertIn(feat["geometry"]["type"], ("Polygon", "MultiPolygon"))

    def test_spatial_filter(self):
        res = self.call("query_features", item_id=STATES_ITEM, out_fields=["STATE_NAME"],
                        near={"lon": -87.6298, "lat": 41.8781, "distance_m": 500})
        self.assertEqual([f["STATE_NAME"] for f in res["features"]], ["Illinois"])
        res = self.call("query_features", item_id=STATES_ITEM, out_fields=["STATE_ABBR"],
                        bbox=[-91, 41.5, -87.5, 42.5])
        self.assertTrue({"IL", "IA", "WI"} <= {f["STATE_ABBR"] for f in res["features"]})

    def test_paging_and_save(self):
        page = self.call("query_features", item_id=STATES_ITEM, out_fields=["STATE_ABBR"], order_by="STATE_ABBR", limit=10)
        self.assertTrue(page["more_available"])
        self.assertEqual(page["next_offset"], 10)
        self.assertGreaterEqual(page["total_matching"], 50)
        nxt = self.call("query_features", item_id=STATES_ITEM, out_fields=["STATE_ABBR"], order_by="STATE_ABBR",
                        limit=10, offset=10)
        self.assertFalse({f["STATE_ABBR"] for f in page["features"]} & {f["STATE_ABBR"] for f in nxt["features"]})

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tracts.csv")
            res = self.call("query_features", item_id=TRACTS_ITEM, where="STATE_ABBR = 'IL'",
                            out_fields=["FIPS"], limit=2500, save_to=path)
            self.assertEqual(res["returned"], 2500, "should page past the 2000-record server limit")
            with open(path) as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 2500)
            self.assertEqual(len({r["FIPS"] for r in rows}), 2500, "pages must not overlap")

    def test_summarize(self):
        total = self.call("summarize_features", item_id=STATES_ITEM,
                          statistics=[{"field": "POPULATION", "type": "sum", "alias": "pop"}])
        self.assertGreater(total["rows"][0]["pop"], 300_000_000)
        top = self.call("summarize_features", item_id=STATES_ITEM, group_by=["STATE_ABBR"],
                        statistics=[{"field": "POPULATION", "type": "sum", "alias": "pop"}], limit=3)
        self.assertEqual(top["rows"][0]["STATE_ABBR"], "CA")
        self.assertEqual(len(top["rows"]), 3)

    def test_helpful_field_errors(self):
        msg = self.call_error("summarize_features", item_id=STATES_ITEM, group_by=["NOT_A_FIELD"])
        self.assertIn("NOT_A_FIELD", msg)
        self.assertIn("STATE_NAME", msg, "error should list valid fields")

    def test_geocode(self):
        res = self.call("geocode", text="Wrigley Field, Chicago", max_results=1)
        top = res["candidates"][0]
        self.assertAlmostEqual(top["lat"], 41.948, delta=0.01)
        self.assertAlmostEqual(top["lon"], -87.655, delta=0.01)
        rev = self.call("reverse_geocode", lon=-87.6553, lat=41.9484)
        self.assertEqual(rev["address"]["City"], "Chicago")


if __name__ == "__main__":
    unittest.main()
