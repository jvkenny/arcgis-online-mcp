"""Write-tool safety gates and payloads, with the HTTP layer mocked out. No network."""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from arcgis_online_mcp import tools  # noqa: E402
from arcgis_online_mcp.server import Server  # noqa: E402

LAYER = "https://services.arcgis.com/abc/arcgis/rest/services/Hydrants/FeatureServer/0"
ITEM = "0123456789abcdef0123456789abcdef"


class FakeClient:
    mode = "password"
    allow_writes = True
    portal = rest = "https://www.arcgis.com/sharing/rest"

    def __init__(self):
        self.calls = []
        self._layer_cache = {}
        self.item = {"id": ITEM, "owner": "jdoe", "access": "private", "type": "Feature Service",
                     "url": LAYER.rsplit("/", 1)[0], "title": "Hydrants"}

    def layer_info(self, url):
        return {"name": "Hydrants", "objectIdField": "OBJECTID", "geometryType": "esriGeometryPoint",
                "capabilities": "Create,Delete,Query,Update,Editing",
                "extent": {"spatialReference": {"wkid": 102100, "latestWkid": 3857}},
                "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}, {"name": "STATUS", "type": "esriFieldTypeString"}]}

    def require_user(self):
        return "jdoe"

    def org_home(self):
        return "https://example.maps.arcgis.com"

    def request(self, url, params=None, files=None, auth=True):
        self.calls.append((url, params or {}))
        if url.endswith("/query") and (params or {}).get("returnIdsOnly"):
            return {"objectIds": [3, 1, 2]}
        if url.endswith("/query"):
            return {"extent": {"xmin": -88, "ymin": 41, "xmax": -87, "ymax": 42}}
        if url.endswith("/applyEdits"):
            return {"addResults": [{"objectId": 10, "success": True}], "updateResults": [],
                    "deleteResults": [{"objectId": i, "success": True} for i in (1, 2, 3)]}
        if "/content/items/" in url and url.endswith(("/share", "/unshare")):
            return {"notSharedWith": []}
        if "/content/items/" in url:
            return dict(self.item)
        if url.endswith("/addItem"):
            return {"success": True, "id": "f" * 32}
        if url == self.item["url"]:
            return {"layers": [{"id": 0, "name": "Hydrants"}], "tables": [{"id": 1, "name": "Inspections"}]}
        return {"success": True}


class WriteGuardTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.server = Server(client=self.client)

    def call(self, name, **args):
        res = self.server.call_tool(name, args)
        return res["isError"], json.loads(res["content"][0]["text"]) if not res["isError"] else res["content"][0]["text"]

    def posted(self, suffix):
        return [p for u, p in self.client.calls if u.endswith(suffix)]

    def test_delete_requires_confirmation(self):
        err, out = self.call("apply_edits", url=LAYER, delete_where="STATUS = 'retired'")
        self.assertFalse(err)
        self.assertIn("blocked", out)
        self.assertEqual(out["deletes"], 3)
        self.assertEqual(self.posted("/applyEdits"), [], "nothing may be sent without confirm_delete")

    def test_dry_run_sends_nothing(self):
        err, out = self.call("apply_edits", url=LAYER, dry_run=True,
                             adds=[{"attributes": {"STATUS": "new"}, "geometry": {"type": "Point", "coordinates": [-87.6, 41.9]}}])
        self.assertTrue(out["dry_run"])
        self.assertEqual(self.posted("/applyEdits"), [])
        geom = out["sample_add_payload"][0]["geometry"]
        self.assertEqual(geom["spatialReference"]["wkid"], 102100, "geometry projected to the layer's SR")

    def test_confirmed_edit_is_transactional(self):
        err, out = self.call("apply_edits", url=LAYER, confirm_delete=True, delete_object_ids=[1, 2, 3],
                             adds=[{"attributes": {"STATUS": "new"}}])
        self.assertFalse(err, out)
        sent = self.posted("/applyEdits")[0]
        self.assertTrue(sent["rollbackOnFailure"])
        self.assertEqual(sent["deletes"], "1,2,3")
        self.assertEqual(out["deletes"]["succeeded"], 3)

    def test_updates_need_object_id(self):
        err, msg = self.call("apply_edits", url=LAYER, updates=[{"attributes": {"STATUS": "x"}}])
        self.assertTrue(err)
        self.assertIn("OBJECTID", msg)

    def test_public_share_requires_confirmation(self):
        err, out = self.call("share_item", item_id=ITEM, access="public")
        self.assertIn("blocked", out)
        self.assertEqual(self.posted("/share"), [])
        err, out = self.call("share_item", item_id=ITEM, access="public", confirm_public=True)
        self.assertTrue(self.posted("/share")[0]["everyone"])

    def test_share_to_group_keeps_access(self):
        self.call("share_item", item_id=ITEM, share_groups=["a" * 32])
        sent = self.posted("/share")[0]
        self.assertFalse(sent["everyone"])
        self.assertFalse(sent["org"])
        self.assertEqual(sent["groups"], "a" * 32)

    def test_webmap_payload(self):
        err, out = self.call("create_webmap", title="Hydrants", layers=[{"item_id": ITEM, "filter": "STATUS='active'"}])
        self.assertFalse(err, out)
        add = self.posted("/addItem")[0]
        wm = add["text"]
        self.assertEqual(add["type"], "Web Map")
        self.assertEqual([l["url"] for l in wm["operationalLayers"]], [LAYER], "service expands to layers, not tables")
        self.assertEqual(wm["operationalLayers"][0]["layerDefinition"]["definitionExpression"], "STATUS='active'")
        self.assertIn("World_Topo_Map", wm["baseMap"]["baseMapLayers"][0]["url"])
        self.assertIn("initialState", wm, "extent computed from the layers")
        self.assertIn("mapviewer", out["map_viewer"])


if __name__ == "__main__":
    unittest.main()
