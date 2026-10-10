"""MCP tool definitions for ArcGIS Online / Enterprise."""

from __future__ import annotations

import csv
import os
import datetime as dt
import html
import json
import re
import time
import uuid
from pathlib import Path

from . import __version__
from .client import ArcGISClient, ArcGISError
from .geometry import WEB_MERCATOR_WKIDS, _to_mercator, bbox_to_envelope, esri_to_geojson, geojson_to_esri

TOOLS = []


class Tool:
    def __init__(self, name, description, schema, fn, *, write=False, destructive=False,
                 idempotent=False, oauth_only=False, title=None):
        self.name = name
        self.description = description
        self.schema = schema
        self.fn = fn
        self.write = write
        self.destructive = destructive
        self.idempotent = idempotent
        self.oauth_only = oauth_only
        self.title = title or name.replace("_", " ").capitalize()

    def spec(self):
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.schema,
            "annotations": {
                "title": self.title,
                "readOnlyHint": not self.write,
                "destructiveHint": self.destructive,
                "idempotentHint": self.idempotent or not self.write,
                "openWorldHint": True,
            },
        }


def tool(name, description, properties=None, required=None, **kwargs):
    schema = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        schema["required"] = required

    def deco(fn):
        TOOLS.append(Tool(name, description, schema, fn, **kwargs))
        return fn

    return deco


def available_tools(client: ArcGISClient):
    out = []
    for t in TOOLS:
        if t.write and not client.allow_writes:
            continue
        if t.oauth_only and client.mode != "oauth":
            continue
        out.append(t)
    return out


# ---------------------------------------------------------------------- helpers

ITEM_ID = {"type": "string", "description": "ArcGIS item ID (32-character hex)."}
LAYER_URL = {
    "type": "string",
    "description": "REST URL of a layer or table, e.g. https://services.arcgis.com/.../FeatureServer/0. "
                   "A service URL without a layer index uses layer 0 (or `layer`).",
}
LAYER_INDEX = {"type": "integer", "minimum": 0, "description": "Layer/table index within the service (default 0)."}
BBOX = {
    "type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
    "description": "Bounding box in WGS84: [min_lon, min_lat, max_lon, max_lat].",
}
_SERVICE_RE = re.compile(r"/(FeatureServer|MapServer|ImageServer|VectorTileServer|SceneServer)(/(\d+))?/?$", re.I)
_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.I)


def _iso(ms):
    if ms in (None, "", -1):
        return None
    try:
        return dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return ms


def _text(value, limit):
    if not value:
        return value
    value = re.sub(r"<[^>]+>", " ", html.unescape(str(value)))
    value = re.sub(r"\s+", " ", html.unescape(value)).strip()
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


def _ftype(t):
    return (t or "").replace("esriFieldType", "")


def _check_id(item_id):
    item_id = (item_id or "").strip()
    if not _ID_RE.match(item_id):
        raise ArcGISError(f"'{item_id}' doesn't look like an ArcGIS item ID (32 hex characters).")
    return item_id


def _get_item(c: ArcGISClient, item_id):
    return c.request(f"{c.rest}/content/items/{_check_id(item_id)}")


def _item_page(c, item_id):
    try:
        base = c.org_home()
    except ArcGISError:
        base = c.portal
    return f"{base}/home/item.html?id={item_id}"


def _item_summary(c, it):
    return {
        "id": it.get("id"),
        "title": it.get("title"),
        "type": it.get("type"),
        "owner": it.get("owner"),
        "snippet": _text(it.get("snippet"), 240),
        "url": it.get("url") or None,
        "access": it.get("access"),
        "modified": _iso(it.get("modified")),
        "tags": (it.get("tags") or [])[:8],
        "views": it.get("numViews"),
        "item_page": _item_page(c, it.get("id")),
    }


def _service_url(c, url=None, item_id=None):
    if url:
        return url.strip().rstrip("/")
    if item_id:
        it = _get_item(c, item_id)
        if not it.get("url"):
            raise ArcGISError(f"Item {item_id} ({it.get('type')}) has no service URL.")
        return it["url"].rstrip("/")
    raise ArcGISError("Provide either url or item_id.")


def _layer_url(c, url=None, item_id=None, layer=None):
    base = _service_url(c, url, item_id)
    m = _SERVICE_RE.search(base)
    if m and m.group(3) is None:
        return f"{base}/{layer if layer is not None else 0}"
    if m is None and item_id is None and not re.search(r"/\d+$", base):
        raise ArcGISError(f"'{base}' doesn't look like a layer URL (expected .../FeatureServer/<n> or .../MapServer/<n>).")
    return base


def _layer_wkid(meta):
    sr = (meta.get("extent") or {}).get("spatialReference") or meta.get("spatialReference") or {}
    return sr.get("latestWkid") or sr.get("wkid") or 4326


def _date_fields(meta):
    return {f["name"] for f in meta.get("fields") or [] if f.get("type") == "esriFieldTypeDate"}


def _convert_attrs(attrs, date_fields):
    if not date_fields:
        return attrs
    return {k: (_iso(v) if k in date_fields and isinstance(v, (int, float)) else v) for k, v in attrs.items()}


def _check_fields(meta, names, what="field"):
    """Fail early with the valid field list; ArcGIS just says 'Unable to perform query'."""
    valid = {f["name"].lower(): f["name"] for f in meta.get("fields") or []}
    if not valid:
        return
    bad = [n for n in names if n and n.lower() not in valid]
    if bad:
        raise ArcGISError(
            f"Unknown {what}(s) {bad} on layer '{meta.get('name')}'. Valid fields: {sorted(valid.values())}"
        )


def _spatial_filter(params, bbox=None, near=None, relationship=None):
    if bbox:
        params["geometry"] = bbox_to_envelope(bbox)
        params["geometryType"] = "esriGeometryEnvelope"
    elif near:
        params["geometry"] = {"x": near["lon"], "y": near["lat"], "spatialReference": {"wkid": 4326}}
        params["geometryType"] = "esriGeometryPoint"
        if near.get("distance_m"):
            params["distance"] = near["distance_m"]
            params["units"] = "esriSRUnit_Meter"
    else:
        return
    params["inSR"] = 4326
    params["spatialRel"] = {
        "intersects": "esriSpatialRelIntersects",
        "contains": "esriSpatialRelContains",
        "within": "esriSpatialRelWithin",
    }[relationship or "intersects"]


NEAR = {
    "type": "object",
    "description": "Point search: features within distance_m meters of (lon, lat), WGS84.",
    "properties": {
        "lon": {"type": "number"}, "lat": {"type": "number"},
        "distance_m": {"type": "number", "minimum": 0},
    },
    "required": ["lon", "lat"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------- identity


@tool(
    "whoami",
    "Show which portal this server talks to, the auth mode, the signed-in user (if any) with "
    "key privileges, and whether write tools are enabled. Call this first if anything is unclear.",
)
def whoami(c: ArcGISClient, a):
    out = {"portal": c.portal, "auth_mode": c.mode, "writes_enabled": c.allow_writes}
    try:
        s = c.portal_self(refresh=True)
    except ArcGISError as exc:
        out["portal_error"] = str(exc)
        if c.mode == "api_key":
            out["note"] = "API keys often can't read portals/self; search/query/geocode still work within the key's scopes."
        return out
    out["org"] = {
        "name": s.get("name"),
        "id": s.get("id"),
        "home": c.org_home() if s.get("urlKey") else None,
        "is_enterprise_portal": bool(s.get("isPortal")),
    }
    user = s.get("user")
    if user:
        privs = set(user.get("privileges") or [])
        out["user"] = {
            "username": user.get("username"),
            "full_name": user.get("fullName"),
            "role": user.get("role"),
            "user_type": user.get("userLicenseTypeId"),
            "can_create_items": "portal:user:createItem" in privs,
            "can_publish_hosted_features": "portal:publisher:publishFeatures" in privs,
            "can_share_public": "portal:user:shareToPublic" in privs,
            "is_admin": user.get("role") == "org_admin",
        }
    else:
        out["user"] = None
        if c.mode == "oauth":
            out["note"] = "Not signed in. Call sign_in to open the ArcGIS login page."
        elif c.mode == "anonymous":
            out["note"] = "Anonymous: public content only. See the README to add credentials."
    return out


@tool(
    "sign_in",
    "Sign in to ArcGIS with OAuth (works with SAML/enterprise logins). Opens the ArcGIS login page "
    "in the user's browser and waits for them to finish. Returns a sign_in_url if the browser didn't open.",
    {"wait_seconds": {"type": "integer", "minimum": 0, "maximum": 300, "description": "How long to wait (default 90)."}},
    oauth_only=True,
)
def sign_in(c: ArcGISClient, a):
    return c.sign_in(wait_seconds=a.get("wait_seconds", 90))


@tool("sign_out", "Forget the cached OAuth tokens for this portal.", oauth_only=True)
def sign_out(c: ArcGISClient, a):
    return c.sign_out()


# ---------------------------------------------------------------------- content discovery


@tool(
    "search_content",
    "Search portal items (maps, layers, apps, files). Uses ArcGIS search syntax in `q` "
    "(e.g. 'parcels', 'title:\"Fire Hydrants\"', 'tags:transportation'). Combine with filters. "
    "Returns item IDs, types, owners, and service URLs for follow-up tools.",
    {
        "q": {"type": "string", "description": "Search text / ArcGIS search syntax."},
        "item_type": {"type": "string", "description": "Exact item type, e.g. 'Feature Service', 'Web Map', 'Web Mapping Application', 'Dashboard', 'CSV'."},
        "owner": {"type": "string", "description": "Owner username."},
        "group_id": {"type": "string", "description": "Only items shared to this group."},
        "my_org_only": {"type": "boolean", "description": "Only items from the signed-in user's organization."},
        "bbox": BBOX,
        "sort_field": {"type": "string", "enum": ["relevance", "modified", "created", "title", "numViews", "avgRating"]},
        "sort_order": {"type": "string", "enum": ["asc", "desc"]},
        "num": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Results per page (default 20)."},
        "start": {"type": "integer", "minimum": 1, "description": "1-based start index for paging (use next_start)."},
    },
)
def search_content(c: ArcGISClient, a):
    parts = []
    if a.get("q"):
        parts.append(f"({a['q']})")
    if a.get("item_type"):
        parts.append(f'type:"{a["item_type"]}"')
    if a.get("owner"):
        parts.append(f"owner:{a['owner']}")
    if a.get("my_org_only"):
        org_id = c.portal_self().get("id")
        if not org_id:
            raise ArcGISError("my_org_only needs a signed-in organization account.")
        parts.append(f"orgid:{org_id}")
    if not parts and not a.get("group_id") and not a.get("bbox"):
        raise ArcGISError("Provide q or at least one filter (item_type, owner, group_id, my_org_only, bbox).")
    params = {
        "q": " AND ".join(parts) if parts else None,
        "num": a.get("num", 20),
        "start": a.get("start", 1),
    }
    if a.get("bbox"):
        params["bbox"] = ",".join(str(v) for v in a["bbox"])
    if a.get("sort_field") and a["sort_field"] != "relevance":
        params["sortField"] = a["sort_field"]
        params["sortOrder"] = a.get("sort_order", "desc")
    if a.get("group_id"):
        gid = _check_id(a["group_id"])
        params["q"] = params["q"] or "*"
        data = c.request(f"{c.rest}/content/groups/{gid}/search", params)
    else:
        data = c.request(f"{c.rest}/search", params)
    next_start = data.get("nextStart", -1)
    return {
        "total": data.get("total"),
        "next_start": next_start if next_start and next_start > 0 else None,
        "items": [_item_summary(c, it) for it in data.get("results") or []],
    }


def _summarize_webmap(data):
    def walk(layers):
        out = []
        for lyr in layers or []:
            entry = {
                "title": lyr.get("title"),
                "layer_type": lyr.get("layerType"),
                "url": lyr.get("url"),
                "item_id": lyr.get("itemId"),
                "visible": lyr.get("visibility", True),
            }
            if lyr.get("opacity", 1) != 1:
                entry["opacity"] = lyr.get("opacity")
            definition = (lyr.get("layerDefinition") or {}).get("definitionExpression")
            if definition:
                entry["filter"] = definition
            if lyr.get("layers") and lyr.get("layerType") == "GroupLayer":
                entry["layers"] = walk(lyr["layers"])
            out.append({k: v for k, v in entry.items() if v is not None})
        return out

    basemap = data.get("baseMap") or {}
    return {
        "basemap": basemap.get("title"),
        "operational_layers": walk(data.get("operationalLayers")),
        "tables": [{"title": t.get("title"), "url": t.get("url"), "item_id": t.get("itemId")} for t in data.get("tables") or []],
        "spatial_reference": (data.get("spatialReference") or {}).get("wkid"),
    }


@tool(
    "get_item",
    "Get an item's details: title, type, owner, description, tags, sharing, URL. For web maps, "
    "also lists the layers inside (with URLs and filters); for services, lists the layers/tables.",
    {"item_id": ITEM_ID},
    required=["item_id"],
)
def get_item(c: ArcGISClient, a):
    it = _get_item(c, a["item_id"])
    out = _item_summary(c, it)
    out.update({
        "description": _text(it.get("description"), 3000),
        "terms_of_use": _text(it.get("licenseInfo"), 600),
        "credits": _text(it.get("accessInformation"), 300),
        "type_keywords": it.get("typeKeywords"),
        "created": _iso(it.get("created")),
        "extent_wgs84": it.get("extent") or None,
        "size_bytes": it.get("size"),
        "tags": it.get("tags"),
    })
    if it.get("type") == "Web Map":
        try:
            out["web_map"] = _summarize_webmap(c.request(f"{c.rest}/content/items/{it['id']}/data"))
        except ArcGISError as exc:
            out["web_map_error"] = str(exc)
    elif it.get("url") and _SERVICE_RE.search(it["url"]):
        try:
            svc = c.request(it["url"].rstrip("/"))
            out["layers"] = [
                {"id": l.get("id"), "name": l.get("name"), "geometry_type": l.get("geometryType")}
                for l in svc.get("layers") or []
            ] or None
            out["tables"] = [{"id": t.get("id"), "name": t.get("name")} for t in svc.get("tables") or []] or None
        except ArcGISError as exc:
            out["service_error"] = str(exc)
    return {k: v for k, v in out.items() if v not in (None, [], "")}


@tool(
    "get_item_data",
    "Get the raw JSON definition behind an item (web map JSON, app/dashboard configuration, etc.). "
    "Large definitions are truncated to max_chars.",
    {"item_id": ITEM_ID, "max_chars": {"type": "integer", "minimum": 500, "maximum": 200000, "description": "Default 20000."}},
    required=["item_id"],
)
def get_item_data(c: ArcGISClient, a):
    item_id = _check_id(a["item_id"])
    try:
        data = c.request(f"{c.rest}/content/items/{item_id}/data")
    except ArcGISError as exc:
        if "Non-JSON" in str(exc):
            return {"item_id": item_id, "note": "This item's data is a file, not JSON. Use its item page to download it.",
                    "item_page": _item_page(c, item_id)}
        raise
    text = json.dumps(data, ensure_ascii=False)
    limit = a.get("max_chars", 20000)
    if len(text) > limit:
        return f"[truncated to {limit} of {len(text)} chars]\n" + text[:limit]
    return data


@tool(
    "list_user_content",
    "List a user's folders and items (defaults to the signed-in user). Use `folder` to look inside one folder.",
    {
        "username": {"type": "string", "description": "Defaults to the signed-in user."},
        "folder": {"type": "string", "description": "Folder title or ID."},
        "num": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Default 50."},
        "start": {"type": "integer", "minimum": 1},
    },
)
def list_user_content(c: ArcGISClient, a):
    user = a.get("username") or c.require_user()
    params = {"num": a.get("num", 50), "start": a.get("start", 1)}
    root = c.request(f"{c.rest}/content/users/{user}", params)
    folders = root.get("folders") or []
    data = root
    if a.get("folder"):
        match = next((f for f in folders if a["folder"] in (f.get("id"), f.get("title"))), None)
        if not match:
            raise ArcGISError(f"No folder '{a['folder']}'. Folders: {[f.get('title') for f in folders]}")
        data = c.request(f"{c.rest}/content/users/{user}/{match['id']}", params)
    next_start = data.get("nextStart", -1)
    return {
        "username": user,
        "folder": a.get("folder") or "(root)",
        "folders": [{"id": f.get("id"), "title": f.get("title")} for f in folders],
        "total": data.get("total"),
        "next_start": next_start if next_start and next_start > 0 else None,
        "items": [_item_summary(c, it) for it in data.get("items") or []],
    }


@tool(
    "search_groups",
    "Find groups. With my_groups=true (default when signed in and no q), lists the signed-in user's groups.",
    {
        "q": {"type": "string", "description": "Group search text."},
        "my_groups": {"type": "boolean"},
        "num": {"type": "integer", "minimum": 1, "maximum": 100},
    },
)
def search_groups(c: ArcGISClient, a):
    def summary(g):
        return {"id": g.get("id"), "title": g.get("title"), "owner": g.get("owner"),
                "access": g.get("access"), "snippet": _text(g.get("snippet"), 200)}

    mine = a.get("my_groups", not a.get("q"))
    if mine:
        user = c.require_user()
        groups = c.request(f"{c.rest}/community/users/{user}").get("groups") or []
        if a.get("q"):
            needle = a["q"].lower()
            groups = [g for g in groups if needle in (g.get("title") or "").lower()]
        return {"groups": [summary(g) for g in groups]}
    data = c.request(f"{c.rest}/community/groups", {"q": a.get("q") or "*", "num": a.get("num", 20)})
    return {"total": data.get("total"), "groups": [summary(g) for g in data.get("results") or []]}


# ---------------------------------------------------------------------- services & layers


@tool(
    "describe_service",
    "Describe a Feature/Map/Image service: description, capabilities, spatial reference, and its layers and tables.",
    {"url": {"type": "string", "description": "Service URL (…/FeatureServer, …/MapServer, …/ImageServer)."}, "item_id": ITEM_ID},
)
def describe_service(c: ArcGISClient, a):
    url = _service_url(c, a.get("url"), a.get("item_id"))
    m = _SERVICE_RE.search(url)
    if m and m.group(3) is not None:
        url = url[: m.start(2)]
    s = c.request(url)
    sr = s.get("spatialReference") or (s.get("fullExtent") or {}).get("spatialReference") or {}
    out = {
        "url": url,
        "service_type": m.group(1) if m else None,
        "name": s.get("name") or s.get("mapName") or s.get("documentInfo", {}).get("Title"),
        "description": _text(s.get("serviceDescription") or s.get("description"), 2000),
        "copyright": _text(s.get("copyrightText"), 300),
        "capabilities": s.get("capabilities"),
        "max_record_count": s.get("maxRecordCount"),
        "spatial_reference": sr.get("latestWkid") or sr.get("wkid"),
        "units": s.get("units"),
        "layers": [
            {k: v for k, v in {
                "id": l.get("id"), "name": l.get("name"), "geometry_type": l.get("geometryType"),
                "type": l.get("type"), "sublayer_ids": l.get("subLayerIds"),
            }.items() if v is not None}
            for l in s.get("layers") or []
        ],
        "tables": [{"id": t.get("id"), "name": t.get("name")} for t in s.get("tables") or []],
        "supports_query": "Query" in (s.get("capabilities") or ""),
    }
    return {k: v for k, v in out.items() if v not in (None, [], "")}


@tool(
    "describe_layer",
    "Describe one layer or table: fields (types, aliases, coded-value domains), geometry type, "
    "record count, WGS84 extent, subtypes, relationships, and capabilities. Read this before querying "
    "so `where` clauses use real field names and codes.",
    {"url": LAYER_URL, "item_id": ITEM_ID, "layer": LAYER_INDEX},
)
def describe_layer(c: ArcGISClient, a):
    url = _layer_url(c, a.get("url"), a.get("item_id"), a.get("layer"))
    meta = c.layer_info(url)
    fields = []
    for f in meta.get("fields") or []:
        entry = {"name": f.get("name"), "type": _ftype(f.get("type"))}
        if f.get("alias") and f["alias"] != f["name"]:
            entry["alias"] = f["alias"]
        if f.get("type") == "esriFieldTypeString" and f.get("length"):
            entry["length"] = f["length"]
        if f.get("nullable") is False:
            entry["nullable"] = False
        if f.get("editable") is False:
            entry["editable"] = False
        dom = f.get("domain")
        if dom and dom.get("type") == "codedValue":
            values = dom.get("codedValues") or []
            entry["domain"] = {
                "name": dom.get("name"),
                "codes": [{"code": v.get("code"), "label": v.get("name")} for v in values[:60]],
            }
            if len(values) > 60:
                entry["domain"]["truncated_from"] = len(values)
        elif dom and dom.get("type") == "range":
            entry["domain"] = {"name": dom.get("name"), "range": dom.get("range")}
        fields.append(entry)

    adv = meta.get("advancedQueryCapabilities") or {}
    out = {
        "url": url,
        "name": meta.get("name"),
        "type": meta.get("type"),
        "geometry_type": meta.get("geometryType"),
        "description": _text(meta.get("description"), 1000),
        "object_id_field": meta.get("objectIdField"),
        "global_id_field": meta.get("globalIdField"),
        "display_field": meta.get("displayField"),
        "spatial_reference": _layer_wkid(meta) if meta.get("geometryType") else None,
        "capabilities": meta.get("capabilities"),
        "max_record_count": meta.get("maxRecordCount"),
        "supports_pagination": adv.get("supportsPagination", meta.get("supportsPagination")),
        "supports_statistics": adv.get("supportsStatistics", meta.get("supportsStatistics")),
        "has_attachments": meta.get("hasAttachments"),
        "last_edit": _iso((meta.get("editingInfo") or {}).get("lastEditDate")),
        "fields": fields,
    }
    if meta.get("types") and meta.get("typeIdField"):
        out["subtypes"] = {
            "field": meta["typeIdField"],
            "values": [{"id": t.get("id"), "name": t.get("name")} for t in meta["types"][:60]],
        }
    if meta.get("relationships"):
        out["relationships"] = [
            {"id": r.get("id"), "name": r.get("name"), "related_table_id": r.get("relatedTableId"),
             "cardinality": r.get("cardinality"), "key_field": r.get("keyField")}
            for r in meta["relationships"]
        ]
    if meta.get("timeInfo"):
        ti = meta["timeInfo"]
        out["time"] = {"start_field": ti.get("startTimeField"), "end_field": ti.get("endTimeField"),
                       "extent": [_iso(v) for v in ti.get("timeExtent") or []]}
    if "Query" in (meta.get("capabilities") or "Query"):
        q = {"where": "1=1", "returnCountOnly": True}
        try:
            res = c.request(f"{url}/query", dict(q, returnExtentOnly=bool(meta.get("geometryType")), outSR=4326))
        except ArcGISError:
            try:
                res = c.request(f"{url}/query", q)
            except ArcGISError as exc:
                res = {"count_error": str(exc)}
        out["record_count"] = res.get("count")
        ext = res.get("extent")
        if ext and ext.get("xmin") is not None and ext.get("xmin") == ext.get("xmin"):
            out["extent_wgs84"] = [round(ext[k], 6) for k in ("xmin", "ymin", "xmax", "ymax")]
        if res.get("count_error"):
            out["record_count_error"] = res["count_error"]
    return {k: v for k, v in out.items() if v not in (None, [], "")}


# ---------------------------------------------------------------------- querying


def _fetch_features(c, url, meta, params, offset, limit):
    """Page through a layer query. Returns (features, more_available)."""
    adv = meta.get("advancedQueryCapabilities") or {}
    paginates = adv.get("supportsPagination", meta.get("supportsPagination", False))
    page = max(1, min(meta.get("maxRecordCount") or 1000, 2000, limit))
    oid = meta.get("objectIdField")
    if paginates and not params.get("orderByFields") and oid and adv.get("supportsOrderBy", True):
        params["orderByFields"] = oid

    features = []
    if paginates:
        while len(features) < limit:
            want = min(page, limit - len(features))
            res = c.request(f"{url}/query", dict(params, resultOffset=offset + len(features), resultRecordCount=want))
            batch = res.get("features") or []
            features.extend(batch)
            if not batch or (len(batch) < want and not res.get("exceededTransferLimit")):
                return features[:limit], False
        return features[:limit], True

    if offset == 0 and limit <= page:
        res = c.request(f"{url}/query", params)
        batch = res.get("features") or []
        return batch[:limit], bool(res.get("exceededTransferLimit")) or len(batch) > limit

    # No pagination support: fetch the ID list, then pull features in ID batches.
    ids_params = {k: v for k, v in params.items() if k not in ("outFields", "returnGeometry", "outSR", "geometryPrecision")}
    ids = c.request(f"{url}/query", dict(ids_params, returnIdsOnly=True)).get("objectIds") or []
    ids = sorted(ids)
    user_order = params.get("orderByFields") and params["orderByFields"] != oid
    if user_order and (offset > 0 or len(ids) > limit):
        raise ArcGISError(
            "This service can't page in a custom order. Raise limit to cover all "
            f"{len(ids)} matches, narrow the where clause, or use summarize_features for top-N questions."
        )
    window = ids[offset: offset + limit]
    for i in range(0, len(window), page):
        chunk = window[i: i + page]
        res = c.request(f"{url}/query", dict(params, objectIds=",".join(str(x) for x in chunk), where=None))
        features.extend(res.get("features") or [])
    return features, offset + limit < len(ids)


def _write_output(path, rows, geojson_features, return_geometry):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in (".geojson", ".json"):
        fc = {"type": "FeatureCollection", "features": geojson_features}
        path.write_text(json.dumps(fc, ensure_ascii=False))
    elif path.suffix.lower() == ".csv":
        columns = []
        for r in rows:
            for k in r:
                if k not in columns:
                    columns.append(k)
        extra = []
        if return_geometry:
            if all((f.get("geometry") or {}).get("type") in (None, "Point") for f in geojson_features):
                extra = ["longitude", "latitude"]
            else:
                extra = ["geometry_geojson"]
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(columns + extra)
            for r, f in zip(rows, geojson_features):
                row = [r.get(k) for k in columns]
                g = f.get("geometry")
                if extra == ["longitude", "latitude"]:
                    row += g["coordinates"][:2] if g else [None, None]
                elif extra:
                    row.append(json.dumps(g) if g else None)
                w.writerow(row)
    else:
        raise ArcGISError("save_to must end in .geojson, .json, or .csv")
    return str(path.resolve())


@tool(
    "query_features",
    "Query features from a layer or table with SQL `where`, optional spatial filter (bbox or near-point), "
    "field selection, ordering, and paging. Geometry is returned as GeoJSON in WGS84 when "
    "return_geometry=true. For big pulls set save_to (a local .geojson/.csv path) and limit up to 100000; "
    "the file gets the full result and only a summary comes back. Dates are returned as ISO-8601 UTC.",
    {
        "url": LAYER_URL, "item_id": ITEM_ID, "layer": LAYER_INDEX,
        "where": {"type": "string", "description": "SQL where clause (default '1=1'). Use real field names from describe_layer."},
        "out_fields": {"type": "array", "items": {"type": "string"}, "description": "Fields to return (default all)."},
        "object_ids": {"type": "array", "items": {"type": "integer"}, "description": "Only these object IDs."},
        "bbox": BBOX,
        "near": NEAR,
        "spatial_relationship": {"type": "string", "enum": ["intersects", "contains", "within"]},
        "order_by": {"type": "string", "description": "e.g. 'POPULATION DESC'."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100000, "description": "Max features (default 50; max 2000 unless save_to)."},
        "offset": {"type": "integer", "minimum": 0},
        "return_geometry": {"type": "boolean", "description": "Default false."},
        "geometry_precision": {"type": "integer", "minimum": 0, "maximum": 10, "description": "Decimal places for coordinates (default 6)."},
        "save_to": {"type": "string", "description": "Write results to this local file (.geojson, .json, or .csv) instead of returning them."},
    },
)
def query_features(c: ArcGISClient, a):
    url = _layer_url(c, a.get("url"), a.get("item_id"), a.get("layer"))
    meta = c.layer_info(url)
    save_to = a.get("save_to")
    limit = a.get("limit", 50)
    if not save_to and limit > 2000:
        raise ArcGISError("limit above 2000 needs save_to (write to a file) to keep responses small.")
    rg = bool(a.get("return_geometry")) and bool(meta.get("geometryType"))
    precision = a.get("geometry_precision", 6)
    out_fields = [f for f in a.get("out_fields") or [] if f != "*"]
    _check_fields(meta, out_fields, "out_field")
    oid_field = meta.get("objectIdField")
    if out_fields and oid_field and oid_field.lower() not in {f.lower() for f in out_fields}:
        out_fields.append(oid_field)  # keep feature IDs so results can be edited/joined later
    params = {
        "where": a.get("where") or "1=1",
        "outFields": ",".join(out_fields) if out_fields else "*",
        "returnGeometry": rg,
        "outSR": 4326,
        "orderByFields": a.get("order_by"),
    }
    if rg:
        params["geometryPrecision"] = precision
    if a.get("object_ids"):
        params["objectIds"] = ",".join(str(i) for i in a["object_ids"])
    _spatial_filter(params, a.get("bbox"), a.get("near"), a.get("spatial_relationship"))

    offset = a.get("offset", 0)
    raw, more = _fetch_features(c, url, meta, params, offset, limit)
    dates = _date_fields(meta)
    oid = meta.get("objectIdField")
    rows = [_convert_attrs(f.get("attributes") or {}, dates) for f in raw]
    gj = [
        {"type": "Feature", "id": r.get(oid), "properties": r,
         "geometry": esri_to_geojson(f.get("geometry"), precision) if rg else None}
        for r, f in zip(rows, raw)
    ]

    out = {"layer": meta.get("name"), "url": url, "returned": len(rows), "offset": offset, "more_available": more}
    if more:
        out["next_offset"] = offset + len(rows)
        try:
            count_params = {k: v for k, v in params.items() if k not in ("outFields", "returnGeometry", "outSR", "orderByFields", "geometryPrecision")}
            out["total_matching"] = c.request(f"{url}/query", dict(count_params, returnCountOnly=True)).get("count")
        except ArcGISError:
            pass
    if save_to:
        out["saved_to"] = _write_output(save_to, rows, gj, rg)
        out["fields"] = list(rows[0].keys()) if rows else []
        out["sample"] = rows[:3]
        return out
    if rg:
        out["geojson"] = {"type": "FeatureCollection", "features": gj}
    else:
        out["features"] = rows
    return out


STAT_TYPES = ["count", "sum", "min", "max", "avg", "stddev", "var"]


@tool(
    "summarize_features",
    "Aggregate a layer server-side: counts, sums, averages, min/max, optionally grouped by fields "
    "(SQL GROUP BY). With group_by and no statistics it returns each distinct value and its count — "
    "use it to list unique values.",
    {
        "url": LAYER_URL, "item_id": ITEM_ID, "layer": LAYER_INDEX,
        "where": {"type": "string", "description": "SQL filter applied before aggregation (default '1=1')."},
        "group_by": {"type": "array", "items": {"type": "string"}},
        "statistics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "type": {"type": "string", "enum": STAT_TYPES},
                    "alias": {"type": "string"},
                },
                "required": ["field", "type"],
                "additionalProperties": False,
            },
            "description": "Defaults to a count of records.",
        },
        "bbox": BBOX,
        "near": NEAR,
        "order_by": {"type": "string", "description": "e.g. 'count DESC' (use output aliases)."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 5000, "description": "Max rows returned (default 200)."},
    },
)
def summarize_features(c: ArcGISClient, a):
    url = _layer_url(c, a.get("url"), a.get("item_id"), a.get("layer"))
    meta = c.layer_info(url)
    oid = meta.get("objectIdField") or "OBJECTID"
    _check_fields(meta, (a.get("group_by") or []) + [s["field"] for s in a.get("statistics") or []])
    stats = []
    for s in a.get("statistics") or [{"field": oid, "type": "count", "alias": "count"}]:
        alias = s.get("alias") or f"{s['type']}_{s['field']}"
        alias = re.sub(r"[^A-Za-z0-9_]", "_", alias)[:60]
        stats.append({"statisticType": s["type"], "onStatisticField": s["field"], "outStatisticFieldName": alias})
    group_by = a.get("group_by") or []
    params = {
        "where": a.get("where") or "1=1",
        "outStatistics": stats,
        "returnGeometry": False,
    }
    if group_by:
        params["groupByFieldsForStatistics"] = ",".join(group_by)
        params["orderByFields"] = a.get("order_by") or f"{stats[0]['outStatisticFieldName']} DESC"
    _spatial_filter(params, a.get("bbox"), a.get("near"))
    try:
        res = c.request(f"{url}/query", params)
    except ArcGISError:
        if not params.get("orderByFields"):
            raise
        params.pop("orderByFields")
        res = c.request(f"{url}/query", params)
    dates = _date_fields(meta)
    rows = [_convert_attrs(f.get("attributes") or {}, dates) for f in res.get("features") or []]
    limit = a.get("limit", 200)
    out = {"layer": meta.get("name"), "groups": len(rows), "rows": rows[:limit]}
    if len(rows) > limit:
        out["truncated"] = True
    if res.get("exceededTransferLimit"):
        out["note"] = "Server hit its transfer limit; some groups may be missing. Narrow the where clause."
    return out


# ---------------------------------------------------------------------- geocoding


def _geocoder(c: ArcGISClient):
    override = os.environ.get("ARCGIS_GEOCODE_URL")
    if override:
        return override.rstrip("/")
    if c.mode == "anonymous":
        return "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer"
    try:
        services = (c.portal_self().get("helperServices") or {}).get("geocode") or []
        if services and services[0].get("url"):
            return services[0]["url"].rstrip("/")
    except ArcGISError:
        pass
    return "https://geocode-api.arcgis.com/arcgis/rest/services/World/GeocodeServer"


@tool(
    "geocode",
    "Find coordinates for an address, place name, or point of interest using the ArcGIS World "
    "Geocoding Service (or your portal's geocoder). Returns ranked candidates in WGS84. "
    "Signed-in/API-key geocoding may consume ArcGIS credits; results are not stored.",
    {
        "text": {"type": "string", "description": "Address or place, e.g. '380 New York St, Redlands CA' or 'Wrigley Field'."},
        "near": {
            "type": "object", "description": "Bias results toward this point.",
            "properties": {"lon": {"type": "number"}, "lat": {"type": "number"}},
            "required": ["lon", "lat"], "additionalProperties": False,
        },
        "country_code": {"type": "string", "description": "ISO country code(s), e.g. 'USA' or 'USA,CAN'."},
        "category": {"type": "string", "description": "Optional category filter, e.g. 'Coffee Shop', 'Address', 'POI'."},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Default 5."},
    },
    required=["text"],
)
def geocode(c: ArcGISClient, a):
    params = {
        "SingleLine": a["text"],
        "maxLocations": a.get("max_results", 5),
        "outFields": "Addr_type,Match_addr,PlaceName,Type,City,Region,CntryName",
        "outSR": 4326,
        "forStorage": False,
    }
    if a.get("near"):
        params["location"] = f"{a['near']['lon']},{a['near']['lat']}"
    if a.get("country_code"):
        params["countryCode"] = a["country_code"]
    if a.get("category"):
        params["category"] = a["category"]
    res = c.request(f"{_geocoder(c)}/findAddressCandidates", params)
    out = []
    for cand in res.get("candidates") or []:
        attrs = cand.get("attributes") or {}
        loc = cand.get("location") or {}
        out.append({k: v for k, v in {
            "address": cand.get("address"),
            "score": cand.get("score"),
            "lon": round(loc["x"], 7) if loc.get("x") is not None else None,
            "lat": round(loc["y"], 7) if loc.get("y") is not None else None,
            "match_type": attrs.get("Addr_type"),
            "place_name": attrs.get("PlaceName") or None,
            "place_type": attrs.get("Type") or None,
            "city": attrs.get("City") or None,
            "region": attrs.get("Region") or None,
            "country": attrs.get("CntryName") or None,
        }.items() if v not in (None, "")})
    return {"candidates": out}


@tool(
    "reverse_geocode",
    "Find the nearest address/place for a WGS84 coordinate.",
    {"lon": {"type": "number"}, "lat": {"type": "number"}},
    required=["lon", "lat"],
)
def reverse_geocode(c: ArcGISClient, a):
    res = c.request(
        f"{_geocoder(c)}/reverseGeocode",
        {"location": {"x": a["lon"], "y": a["lat"], "spatialReference": {"wkid": 4326}}, "outSR": 4326},
    )
    addr = res.get("address") or {}
    keep = ("Match_addr", "LongLabel", "PlaceName", "Address", "Neighborhood", "City", "Subregion",
            "Region", "Postal", "CountryCode", "Addr_type", "Type")
    return {"address": {k: addr[k] for k in keep if addr.get(k)}, "location": res.get("location")}


# ---------------------------------------------------------------------- writes (opt-in)


def _feature_list(items, wkid, need_oid=None):
    out = []
    for f in items or []:
        attrs = dict(f.get("attributes") or f.get("properties") or {})
        if need_oid and need_oid not in attrs:
            raise ArcGISError(f"Every update needs its '{need_oid}' in attributes.")
        feat = {"attributes": attrs}
        if f.get("geometry"):
            feat["geometry"] = geojson_to_esri(f["geometry"], wkid)
        out.append(feat)
    return out


EDIT_FEATURE = {
    "type": "object",
    "properties": {
        "attributes": {"type": "object", "description": "Field name → value."},
        "geometry": {"type": "object", "description": "GeoJSON geometry in WGS84 (projected for you)."},
    },
}


@tool(
    "apply_edits",
    "Add, update, and/or delete features in an editable layer, applied as one transaction "
    "(rolled back on any failure). Geometries are GeoJSON in WGS84. Updates must include the "
    "object ID field. Deletes require confirm_delete=true; run with dry_run=true first to preview counts.",
    {
        "url": LAYER_URL, "item_id": ITEM_ID, "layer": LAYER_INDEX,
        "adds": {"type": "array", "items": EDIT_FEATURE, "maxItems": 1000},
        "updates": {"type": "array", "items": EDIT_FEATURE, "maxItems": 1000},
        "delete_object_ids": {"type": "array", "items": {"type": "integer"}, "maxItems": 1000},
        "delete_where": {"type": "string", "description": "Delete every feature matching this SQL (max 1000 deletes per call)."},
        "confirm_delete": {"type": "boolean", "description": "Must be true for any delete to run."},
        "dry_run": {"type": "boolean", "description": "Validate and preview without changing data."},
    },
    write=True, destructive=True,
)
def apply_edits(c: ArcGISClient, a):
    url = _layer_url(c, a.get("url"), a.get("item_id"), a.get("layer"))
    meta = c.layer_info(url)
    caps = (meta.get("capabilities") or "").lower()
    wkid = _layer_wkid(meta)
    oid = meta.get("objectIdField")
    adds = _feature_list(a.get("adds"), wkid)
    updates = _feature_list(a.get("updates"), wkid, need_oid=oid)

    delete_ids = list(a.get("delete_object_ids") or [])
    if a.get("delete_where"):
        matched = c.request(f"{url}/query", {"where": a["delete_where"], "returnIdsOnly": True}).get("objectIds") or []
        delete_ids = sorted(set(delete_ids) | set(matched))
    if len(delete_ids) > 1000:
        raise ArcGISError(f"That would delete {len(delete_ids)} features; the limit is 1000 per call. Narrow it.")

    for label, items, cap in (("adds", adds, "create"), ("updates", updates, "update"), ("deletes", delete_ids, "delete")):
        if items and cap not in caps and "editing" not in caps:
            raise ArcGISError(f"Layer capabilities '{meta.get('capabilities')}' don't allow {label}.")
    if not (adds or updates or delete_ids):
        return {"note": "Nothing to do (no adds, updates, or matched deletes)."}

    preview = {
        "layer": meta.get("name"), "url": url, "layer_wkid": wkid,
        "adds": len(adds), "updates": len(updates), "deletes": len(delete_ids),
        "delete_object_ids": delete_ids[:50],
    }
    if a.get("dry_run"):
        preview["dry_run"] = True
        preview["sample_add_payload"] = adds[:2]
        return preview
    if delete_ids and not a.get("confirm_delete"):
        preview["blocked"] = "Deletes need confirm_delete=true. Nothing was changed."
        return preview

    res = c.request(f"{url}/applyEdits", {
        "adds": adds or None,
        "updates": updates or None,
        "deletes": ",".join(str(i) for i in delete_ids) or None,
        "rollbackOnFailure": True,
    })

    def tally(results):
        results = results or []
        failed = [r for r in results if not r.get("success")]
        return {
            "succeeded": len(results) - len(failed),
            "failed": len(failed),
            "object_ids": [r.get("objectId") for r in results if r.get("success")][:200],
            "errors": [r.get("error") for r in failed][:10],
        }

    c._layer_cache.pop(url, None)
    return {"layer": meta.get("name"), "adds": tally(res.get("addResults")),
            "updates": tally(res.get("updateResults")), "deletes": tally(res.get("deleteResults"))}


def _service_name(c, title):
    base = re.sub(r"[^A-Za-z0-9_]", "_", title).strip("_")[:80] or "layer"
    if base[0].isdigit():
        base = "L_" + base
    org_id = c.portal_self().get("id")
    if not org_id:
        return f"{base}_{uuid.uuid4().hex[:6]}"
    for i in range(6):
        name = base if i == 0 else f"{base}_{uuid.uuid4().hex[:6]}"
        res = c.request(f"{c.rest}/portals/{org_id}/isServiceNameAvailable", {"name": name, "type": "Feature Service"})
        if res.get("available"):
            return name
    raise ArcGISError("Couldn't find an available service name; pass a different title or service_name.")


@tool(
    "create_hosted_layer",
    "Publish GeoJSON (WGS84) as a new hosted feature layer owned by the signed-in user. Pass a "
    "FeatureCollection inline (`geojson`) or a local file path (`geojson_path`). The new layer is private.",
    {
        "title": {"type": "string"},
        "geojson": {"type": "object", "description": "A GeoJSON FeatureCollection."},
        "geojson_path": {"type": "string", "description": "Path to a local .geojson file."},
        "service_name": {"type": "string", "description": "Optional REST service name (letters, digits, underscores)."},
        "tags": {"type": "array", "items": {"type": "string"}},
        "snippet": {"type": "string", "description": "One-line summary."},
        "description": {"type": "string"},
    },
    required=["title"], write=True,
)
def create_hosted_layer(c: ArcGISClient, a):
    user = c.require_user()
    if a.get("geojson_path"):
        path = Path(a["geojson_path"]).expanduser()
        if path.stat().st_size > 100 * 1024 * 1024:
            raise ArcGISError("File is over 100 MB; publish it through the ArcGIS web UI instead.")
        payload = path.read_bytes()
        fc = json.loads(payload)
    elif a.get("geojson"):
        fc = a["geojson"]
        payload = json.dumps(fc).encode("utf-8")
    else:
        raise ArcGISError("Provide geojson or geojson_path.")
    if fc.get("type") != "FeatureCollection" or not fc.get("features"):
        raise ArcGISError("GeoJSON must be a FeatureCollection with at least one feature.")

    tags = ",".join(a.get("tags") or ["arcgis-online-mcp"])
    added = c.request(
        f"{c.rest}/content/users/{user}/addItem",
        {"type": "GeoJson", "title": a["title"], "tags": tags, "snippet": a.get("snippet"),
         "description": a.get("description")},
        files={"file": (re.sub(r"[^A-Za-z0-9_.-]", "_", a["title"]) + ".geojson", payload, "application/geo+json")},
    )
    source_id = added.get("id")
    if not source_id:
        raise ArcGISError(f"addItem failed: {added}")

    name = a.get("service_name") or _service_name(c, a["title"])
    pub = c.request(
        f"{c.rest}/content/users/{user}/publish",
        {"itemid": source_id, "filetype": "geojson",
         "publishParameters": {"name": name, "maxRecordCount": 2000, "hasStaticData": False}},
    )
    svc = (pub.get("services") or [{}])[0]
    if svc.get("error") or not svc.get("serviceItemId"):
        raise ArcGISError(f"Publish failed: {svc.get('error') or pub}. Source GeoJSON item {source_id} was kept.")

    item_id, job_id = svc["serviceItemId"], svc.get("jobId")
    status = "unknown"
    deadline = time.time() + 180
    while job_id and time.time() < deadline:
        st = c.request(f"{c.rest}/content/users/{user}/items/{item_id}/status", {"jobId": job_id, "jobType": "publish"})
        status = st.get("status")
        if status in ("completed", "failed"):
            if status == "failed":
                raise ArcGISError(f"Publish job failed: {st.get('statusMessage')}")
            break
        time.sleep(2)
    service_url = svc.get("serviceurl") or svc.get("serviceUrl")
    return {
        "status": status if job_id else "completed",
        "service_item_id": item_id,
        "service_url": service_url,
        "layer_url": f"{service_url}/0" if service_url else None,
        "item_page": _item_page(c, item_id),
        "source_geojson_item_id": source_id,
        "access": "private",
        "features_submitted": len(fc["features"]),
    }


BASEMAPS = {
    "topographic": ("World Topographic Map", ["https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer"]),
    "imagery": ("World Imagery", ["https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer"]),
    "streets": ("World Street Map", ["https://services.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer"]),
    "light-gray": ("Light Gray Canvas", [
        "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer",
        "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Reference/MapServer"]),
    "dark-gray": ("Dark Gray Canvas", [
        "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer",
        "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer"]),
    "oceans": ("Oceans", ["https://services.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer"]),
}


def _layer_type(url):
    m = _SERVICE_RE.search(url)
    if not m:
        return "ArcGISFeatureLayer"
    kind, idx = m.group(1).lower(), m.group(3)
    if kind == "imageserver":
        return "ArcGISImageServiceLayer"
    if kind == "mapserver" and idx is None:
        return "ArcGISMapServiceLayer"
    if kind == "vectortileserver":
        return "VectorTileLayer"
    return "ArcGISFeatureLayer"


@tool(
    "create_webmap",
    "Create a web map item from layers (by URL or item ID) on a basemap, ready to open in Map Viewer. "
    "Feature service items expand to all their layers. The new map is private.",
    {
        "title": {"type": "string"},
        "layers": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "item_id": {"type": "string"},
                    "title": {"type": "string"},
                    "visible": {"type": "boolean"},
                    "opacity": {"type": "number", "minimum": 0, "maximum": 1},
                    "filter": {"type": "string", "description": "Definition expression (SQL where)."},
                },
                "additionalProperties": False,
            },
            "description": "Bottom-to-top draw order.",
        },
        "basemap": {"type": "string", "enum": list(BASEMAPS), "description": "Default topographic."},
        "bbox": BBOX,
        "tags": {"type": "array", "items": {"type": "string"}},
        "snippet": {"type": "string"},
        "description": {"type": "string"},
    },
    required=["title", "layers"], write=True,
)
def create_webmap(c: ArcGISClient, a):
    user = c.require_user()
    op_layers = []
    for spec in a["layers"]:
        entries = []
        if spec.get("item_id"):
            it = _get_item(c, spec["item_id"])
            url = (it.get("url") or "").rstrip("/")
            if not url:
                raise ArcGISError(f"Item {spec['item_id']} has no service URL to put on a map.")
            m = _SERVICE_RE.search(url)
            if it.get("type") == "Feature Service" and m and m.group(3) is None:
                svc = c.request(url)
                for lyr in (svc.get("layers") or []) + (svc.get("tables") or []):
                    if lyr in (svc.get("tables") or []):
                        continue
                    entries.append((f"{url}/{lyr['id']}", lyr.get("name"), it["id"]))
            else:
                entries.append((url, spec.get("title") or it.get("title"), it["id"]))
        elif spec.get("url"):
            entries.append((spec["url"].rstrip("/"), spec.get("title"), None))
        else:
            raise ArcGISError("Each layer needs url or item_id.")
        for url, title, item_id in entries:
            if not title:
                try:
                    title = c.layer_info(url).get("name")
                except ArcGISError:
                    title = url.rsplit("/", 2)[-2]
            layer = {
                "id": uuid.uuid4().hex[:12],
                "title": spec.get("title") if len(entries) == 1 and spec.get("title") else title,
                "url": url,
                "layerType": _layer_type(url),
                "visibility": spec.get("visible", True),
                "opacity": spec.get("opacity", 1),
            }
            if item_id:
                layer["itemId"] = item_id
            if spec.get("filter"):
                layer["layerDefinition"] = {"definitionExpression": spec["filter"]}
            op_layers.append(layer)

    bm_title, bm_urls = BASEMAPS[a.get("basemap", "topographic")]
    webmap = {
        "operationalLayers": op_layers,
        "baseMap": {
            "title": bm_title,
            "baseMapLayers": [
                {"id": f"basemap_{i}", "title": bm_title, "url": u, "layerType": "ArcGISTiledMapServiceLayer",
                 "visibility": True, "opacity": 1, **({"isReference": True} if "Reference" in u else {})}
                for i, u in enumerate(bm_urls)
            ],
        },
        "spatialReference": {"wkid": 102100, "latestWkid": 3857},
        "version": "2.31",
        "authoringApp": "arcgis-online-mcp",
        "authoringAppVersion": __version__,
    }

    bbox = a.get("bbox")
    if not bbox:
        boxes = []
        for lyr in op_layers:
            if lyr["layerType"] != "ArcGISFeatureLayer":
                continue
            try:
                ext = c.request(f"{lyr['url']}/query", {"where": "1=1", "returnExtentOnly": True, "outSR": 4326}).get("extent") or {}
                if ext.get("xmin") is not None and ext["xmin"] == ext["xmin"]:
                    boxes.append([ext["xmin"], ext["ymin"], ext["xmax"], ext["ymax"]])
            except ArcGISError:
                pass
        if boxes:
            bbox = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
    if bbox:
        lo, hi = _to_mercator([bbox[0], bbox[1]]), _to_mercator([bbox[2], bbox[3]])
        webmap["initialState"] = {"viewpoint": {"targetGeometry": {
            "xmin": lo[0], "ymin": lo[1], "xmax": hi[0], "ymax": hi[1],
            "spatialReference": {"wkid": 102100}}}}

    res = c.request(f"{c.rest}/content/users/{user}/addItem", {
        "type": "Web Map",
        "typeKeywords": "ArcGIS Online,Explorer Web Map,Map,Online Map,Web Map",
        "title": a["title"],
        "tags": ",".join(a.get("tags") or ["arcgis-online-mcp"]),
        "snippet": a.get("snippet"),
        "description": a.get("description"),
        "extent": ",".join(str(round(v, 6)) for v in bbox) if bbox else None,
        "text": webmap,
    })
    item_id = res.get("id")
    if not item_id:
        raise ArcGISError(f"addItem failed: {res}")
    home = _item_page(c, item_id).split("/home/")[0]
    return {
        "item_id": item_id,
        "item_page": _item_page(c, item_id),
        "map_viewer": f"{home}/apps/mapviewer/index.html?webmap={item_id}",
        "layers": [{"title": l["title"], "url": l["url"], "layer_type": l["layerType"]} for l in op_layers],
        "access": "private",
    }


@tool(
    "update_item",
    "Update an item's metadata: title, summary (snippet), description, tags, credits, terms of use.",
    {
        "item_id": ITEM_ID,
        "title": {"type": "string"},
        "snippet": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Replaces all existing tags."},
        "credits": {"type": "string"},
        "terms_of_use": {"type": "string"},
    },
    required=["item_id"], write=True, idempotent=True,
)
def update_item(c: ArcGISClient, a):
    it = _get_item(c, a["item_id"])
    changes = {
        "title": a.get("title"),
        "snippet": a.get("snippet"),
        "description": a.get("description"),
        "tags": ",".join(a["tags"]) if a.get("tags") is not None else None,
        "accessInformation": a.get("credits"),
        "licenseInfo": a.get("terms_of_use"),
    }
    changes = {k: v for k, v in changes.items() if v is not None}
    if not changes:
        return {"note": "Nothing to update."}
    res = c.request(f"{c.rest}/content/users/{it['owner']}/items/{it['id']}/update", changes)
    return {"success": bool(res.get("success")), "item_id": it["id"], "updated": sorted(changes)}


@tool(
    "share_item",
    "Change who can see an item: set access (private / org / public) and/or share to or unshare from "
    "groups. Making something public requires confirm_public=true.",
    {
        "item_id": ITEM_ID,
        "access": {"type": "string", "enum": ["private", "org", "public"], "description": "Omit to keep the current level."},
        "share_groups": {"type": "array", "items": {"type": "string"}, "description": "Group IDs to share to."},
        "unshare_groups": {"type": "array", "items": {"type": "string"}, "description": "Group IDs to remove."},
        "confirm_public": {"type": "boolean"},
    },
    required=["item_id"], write=True, idempotent=True,
)
def share_item(c: ArcGISClient, a):
    it = _get_item(c, a["item_id"])
    current = it.get("access")
    access = a.get("access") or {"public": "public", "org": "org"}.get(current, "private")
    if access == "public" and current != "public" and not a.get("confirm_public"):
        return {"blocked": "Making an item public needs confirm_public=true. Nothing was changed.",
                "item_id": it["id"], "current_access": current}
    out = {"item_id": it["id"], "previous_access": current}
    res = c.request(f"{c.rest}/content/items/{it['id']}/share", {
        "everyone": access == "public",
        "org": access in ("org", "public"),
        "groups": ",".join(_check_id(g) for g in a.get("share_groups") or []) or None,
    })
    out["not_shared_with"] = res.get("notSharedWith") or None
    if a.get("unshare_groups"):
        un = c.request(f"{c.rest}/content/items/{it['id']}/unshare", {
            "groups": ",".join(_check_id(g) for g in a["unshare_groups"]),
        })
        out["not_unshared_from"] = un.get("notUnsharedFrom") or None
    out["access"] = _get_item(c, it["id"]).get("access")
    return {k: v for k, v in out.items() if v is not None}
