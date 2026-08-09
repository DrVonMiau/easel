#!/usr/bin/env python3
"""Regenerate src/worldmap.bin — the offline Map view's vector data.

The Map view draws a built-in Natural Earth 1:10m vector world: land, lakes,
country borders, major rivers, major highways and major cities. There are no
tiles, no network and no image decoding at runtime (all unreliable in the
Flatpak sandbox); the geometry is simplified (Douglas-Peucker) and quantised to
1/100 degree, packed into length-prefixed sections and zlib-compressed.

This tool is *additive*: it keeps the land / lakes / borders geometry already in
src/worldmap.bin (so the basemap that looks right is never disturbed) and adds
the rivers / roads / cities layers from Natural Earth GeoJSON.

Usage:
    # Fetch the three source layers once (≈75 MB) into ./worldmap-data:
    #   base=https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson
    #   for f in ne_10m_rivers_lake_centerlines ne_10m_roads ne_10m_populated_places; do
    #       curl -L -o worldmap-data/$f.geojson $base/$f.geojson
    #   done
    python3 tools/build_worldmap.py            # writes src/worldmap.bin in place

Set WORLDMAP_DATA to point elsewhere for the GeoJSON directory.
"""
import json
import os
import struct
import zlib

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(_HERE, "..", "src", "worldmap.bin")
_DATA = os.environ.get("WORLDMAP_DATA", os.path.join(os.getcwd(), "worldmap-data"))
_VERSION = 2

# Layer selection (Natural Earth ranks; lower = more prominent).
_RIVER_MAX_RANK = 7          # major rivers only
_ROAD_TYPES = {"Major Highway"}
_ROAD_MAX_RANK = 7
_CITY_MAX_RANK = 4           # world/regional cities, with names
_SIMPLIFY_EPS = 0.03         # Douglas-Peucker tolerance, degrees


# ---------- read the existing basemap (v1: 3 sections, or v2: version byte) ----------
def _parse_polylines(data, off):
    (count,) = struct.unpack_from("<I", data, off)
    off += 4
    items = []
    for _ in range(count):
        (npts,) = struct.unpack_from("<H", data, off)
        off += 2
        vals = struct.unpack_from("<%dh" % (npts * 2), data, off)
        off += npts * 4
        items.append([(vals[i] / 100.0, vals[i + 1] / 100.0)
                      for i in range(0, npts * 2, 2)])
    return items, off


def read_basemap():
    raw = zlib.decompress(open(_BIN, "rb").read())
    off = 1 if raw and raw[0] == _VERSION else 0
    secs = []
    for _ in range(3):  # land, lakes, borders
        (slen,) = struct.unpack_from("<I", raw, off)
        off += 4
        items, _end = _parse_polylines(raw, off)
        off += slen
        secs.append(items)
    return secs


# ---------- geometry ----------
def _perp2(p, a, b):
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    d2 = dx * dx + dy * dy
    if d2 == 0:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / d2))
    cx, cy = ax + t * dx, ay + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2


def simplify(pts, eps):
    if len(pts) < 3:
        return pts
    e2 = eps * eps
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        dmax, idx = 0.0, -1
        for k in range(i + 1, j):
            d = _perp2(pts[k], pts[i], pts[j])
            if d > dmax:
                dmax, idx = d, k
        if idx != -1 and dmax > e2:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(pts, keep) if k]


def quantise(pts):
    out, last = [], None
    for lon, lat in pts:
        q = (int(round(lon * 100)), int(round(lat * 100)))
        if q != last:
            out.append(q)
            last = q
    return out


def _geom_lines(geom):
    t = geom["type"]
    if t == "LineString":
        return [geom["coordinates"]]
    if t == "MultiLineString":
        return list(geom["coordinates"])
    return []


def build_lines(path, keep_fn, eps):
    d = json.load(open(path))
    lines = []
    for f in d["features"]:
        if not keep_fn(f["properties"]):
            continue
        for part in _geom_lines(f["geometry"]):
            q = quantise(simplify([(p[0], p[1]) for p in part if len(p) >= 2], eps))
            if len(q) >= 2:
                lines.append(q)
    return lines


def build_cities(path, keep_fn):
    d = json.load(open(path))
    out = []
    for f in d["features"]:
        p = f["properties"]
        if not keep_fn(p):
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        name = (p.get("NAME") or p.get("NAMEASCII") or "").strip()
        if not name:
            continue
        rank = p.get("SCALERANK")
        rank = 10 if rank is None else int(rank)
        out.append((int(round(lon * 100)), int(round(lat * 100)), rank, name))
    out.sort(key=lambda c: c[2])  # most-major first
    return out


# ---------- pack ----------
def _requantise(items):
    lines = []
    for ring in items:
        q = quantise(ring)
        if len(q) >= 2:
            lines.append(q)
    return lines


def pack_polylines(lines):
    body = bytearray(struct.pack("<I", len(lines)))
    for pts in lines:
        body += struct.pack("<H", len(pts))
        for x, y in pts:
            body += struct.pack("<hh", x, y)
    return bytes(body)


def pack_cities(cities):
    body = bytearray(struct.pack("<I", len(cities)))
    for x, y, rank, name in cities:
        nb = name.encode("utf-8")[:255]
        body += struct.pack("<hhBB", x, y, min(rank, 255), len(nb))
        body += nb
    return bytes(body)


def _section(body):
    return struct.pack("<I", len(body)) + body


def _data(name):
    return os.path.join(_DATA, name + ".geojson")


def main():
    land, lakes, borders = read_basemap()
    land = _requantise(land)
    lakes = _requantise(lakes)
    borders = _requantise(borders)

    rivers = build_lines(
        _data("ne_10m_rivers_lake_centerlines"),
        lambda p: p.get("scalerank") is not None and p["scalerank"] <= _RIVER_MAX_RANK,
        _SIMPLIFY_EPS)
    roads = build_lines(
        _data("ne_10m_roads"),
        lambda p: p.get("type") in _ROAD_TYPES and (p.get("scalerank") or 99) <= _ROAD_MAX_RANK,
        _SIMPLIFY_EPS)
    cities = build_cities(
        _data("ne_10m_populated_places"),
        lambda p: p.get("SCALERANK") is not None and p["SCALERANK"] <= _CITY_MAX_RANK)

    payload = bytes([_VERSION])
    for body in (pack_polylines(land), pack_polylines(lakes), pack_polylines(borders),
                 pack_polylines(rivers), pack_polylines(roads), pack_cities(cities)):
        payload += _section(body)
    comp = zlib.compress(payload, 9)
    with open(_BIN, "wb") as fh:
        fh.write(comp)

    def n(lines):
        return sum(len(l) for l in lines)
    print(f"land {len(land)}/{n(land)}  lakes {len(lakes)}/{n(lakes)}  "
          f"borders {len(borders)}/{n(borders)}")
    print(f"rivers {len(rivers)}/{n(rivers)}  roads {len(roads)}/{n(roads)}  "
          f"cities {len(cities)}")
    print(f"compressed {len(comp):,} bytes -> {os.path.normpath(_BIN)}")


if __name__ == "__main__":
    main()
