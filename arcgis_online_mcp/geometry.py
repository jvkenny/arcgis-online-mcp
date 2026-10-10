"""Esri JSON <-> GeoJSON conversion and Web Mercator projection.

Kept dependency-free on purpose: query results come back as Esri JSON in WGS84
(outSR=4326), and edits arrive as GeoJSON in WGS84 that may need projecting into
the layer's spatial reference before applyEdits.
"""

from __future__ import annotations

import math

WEB_MERCATOR_WKIDS = {102100, 3857, 102113, 900913}
WGS84_WKIDS = {4326}
_R = 6378137.0
_MAX_LAT = 85.0511287798


def _ring_area(ring):
    """Signed area (shoelace). Negative = clockwise in a y-up coordinate system."""
    total = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _round(coords, precision):
    if precision is None:
        return coords
    if coords and isinstance(coords[0], (int, float)):
        return [round(c, precision) for c in coords]
    return [_round(c, precision) for c in coords]


def esri_to_geojson(geom, precision=None):
    """Convert an Esri JSON geometry to a GeoJSON geometry (or None)."""
    if not geom:
        return None
    if "x" in geom:
        if geom.get("x") is None:
            return None
        coords = [geom["x"], geom["y"]]
        if geom.get("z") is not None:
            coords.append(geom["z"])
        return {"type": "Point", "coordinates": _round(coords, precision)}
    if "points" in geom:
        return {"type": "MultiPoint", "coordinates": _round(geom["points"], precision)}
    if "paths" in geom:
        paths = geom["paths"]
        if len(paths) == 1:
            return {"type": "LineString", "coordinates": _round(paths[0], precision)}
        return {"type": "MultiLineString", "coordinates": _round(paths, precision)}
    if "rings" in geom:
        # Esri: clockwise rings are exteriors, counter-clockwise rings are holes.
        polygons = []
        for ring in geom["rings"]:
            if len(ring) < 4:
                continue
            if _ring_area(ring) <= 0 or not polygons:
                exterior = ring if _ring_area(ring) >= 0 else list(reversed(ring))
                polygons.append([exterior])
            else:
                hole = ring if _ring_area(ring) <= 0 else list(reversed(ring))
                polygons[-1].append(hole)
        polygons = [_round(p, precision) for p in polygons]
        if not polygons:
            return None
        if len(polygons) == 1:
            return {"type": "Polygon", "coordinates": polygons[0]}
        return {"type": "MultiPolygon", "coordinates": polygons}
    if "xmin" in geom:
        x0, y0, x1, y1 = geom["xmin"], geom["ymin"], geom["xmax"], geom["ymax"]
        ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
        return {"type": "Polygon", "coordinates": _round([ring], precision)}
    return None


def _to_mercator(pt):
    lon, lat = pt[0], max(-_MAX_LAT, min(_MAX_LAT, pt[1]))
    x = _R * math.radians(lon)
    y = _R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return [x, y] + list(pt[2:])


def _map_coords(coords, fn):
    if coords and isinstance(coords[0], (int, float)):
        return fn(coords)
    return [_map_coords(c, fn) for c in coords]


def geojson_to_esri(geom, target_wkid=4326):
    """Convert a WGS84 GeoJSON geometry to Esri JSON in the target spatial reference.

    WGS84 and Web Mercator targets are handled locally. Any other target keeps the
    coordinates in WGS84 and tags them with wkid 4326 so the server can project.
    """
    if not geom:
        return None
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype is None or coords is None:
        raise ValueError("GeoJSON geometry needs 'type' and 'coordinates'")

    if target_wkid in WEB_MERCATOR_WKIDS:
        coords = _map_coords(coords, _to_mercator)
        sr = {"wkid": 102100, "latestWkid": 3857}
    else:
        sr = {"wkid": 4326}

    if gtype == "Point":
        out = {"x": coords[0], "y": coords[1]}
        if len(coords) > 2:
            out["z"] = coords[2]
    elif gtype == "MultiPoint":
        out = {"points": coords}
    elif gtype == "LineString":
        out = {"paths": [coords]}
    elif gtype == "MultiLineString":
        out = {"paths": coords}
    elif gtype in ("Polygon", "MultiPolygon"):
        polys = [coords] if gtype == "Polygon" else coords
        rings = []
        for poly in polys:
            for i, ring in enumerate(poly):
                area = _ring_area(ring)
                # Exterior must be clockwise (negative area), holes counter-clockwise.
                if (i == 0 and area > 0) or (i > 0 and area < 0):
                    ring = list(reversed(ring))
                rings.append(ring)
        out = {"rings": rings}
    else:
        raise ValueError(f"Unsupported GeoJSON geometry type: {gtype}")
    out["spatialReference"] = sr
    return out


def bbox_to_envelope(bbox):
    xmin, ymin, xmax, ymax = [float(v) for v in bbox]
    return {
        "xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
        "spatialReference": {"wkid": 4326},
    }
