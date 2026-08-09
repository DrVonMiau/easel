"""Offline vector world map data for the Map view.

A compact set of Natural Earth 1:10m geometry so the Map view can draw a
detailed, resolution-independent world with no map tiles, no network and no
image decoding (which is what fails in the sandbox). Everything is simplified
(Douglas-Peucker) and quantised to 1/100 degree, packed into length-prefixed
sections and zlib-compressed into worldmap.bin next to this module, decoded
lazily on first use. (See tools/build_worldmap.py for how it's generated.)

Accessors returning lists of point sequences in (lon, lat) degrees:
  land_rings()   — filled land (islands/lakes are separate rings; even-odd)
  lake_rings()   — major lakes, drawn back in the sea colour
  border_lines() — country boundaries, stroked (open polylines)
  river_lines()  — major rivers, stroked in the water colour
  road_lines()   — major highways, stroked faintly (context when zoomed in)
And cities() → list of (lon, lat, rank, name); lower rank = more major.

The binary starts with a version byte, then six length-prefixed sections in
the order above (the five geometry sections, then cities).
"""
import os
import struct
import zlib

_BIN = os.path.join(os.path.dirname(__file__), "worldmap.bin")
_VERSION = 2

_land = None
_lakes = None
_borders = None
_rivers = None
_roads = None
_cities = None


def _parse_section(data, off):
    """A polyline section: uint32 count, then each item = uint16 point count +
    that many (int16 lon*100, int16 lat*100) pairs. Returns (items, new_off)."""
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


def _parse_cities(data, off):
    """Cities section: uint32 count, then each = int16 lon*100, int16 lat*100,
    uint8 rank, uint8 name-length, name (utf-8)."""
    (count,) = struct.unpack_from("<I", data, off)
    off += 4
    items = []
    for _ in range(count):
        x, y, rank, nlen = struct.unpack_from("<hhBB", data, off)
        off += 6
        name = data[off:off + nlen].decode("utf-8", "replace")
        off += nlen
        items.append((x / 100.0, y / 100.0, rank, name))
    return items, off


def _set_empty():
    global _land, _lakes, _borders, _rivers, _roads, _cities
    _land = _lakes = _borders = _rivers = _roads = _cities = []


def _load():
    global _land, _lakes, _borders, _rivers, _roads, _cities
    if _land is not None:
        return
    try:
        with open(_BIN, "rb") as fh:
            raw = zlib.decompress(fh.read())
    except (OSError, zlib.error):
        _set_empty()
        return
    if not raw or raw[0] != _VERSION:
        _set_empty()
        return
    off = 1
    poly = []
    try:
        for _ in range(5):  # land, lakes, borders, rivers, roads
            (slen,) = struct.unpack_from("<I", raw, off)
            off += 4
            items, _end = _parse_section(raw, off)
            off += slen
            poly.append(items)
        (slen,) = struct.unpack_from("<I", raw, off)
        off += 4
        cities, _end = _parse_cities(raw, off)
    except struct.error:
        _set_empty()
        return
    _land, _lakes, _borders, _rivers, _roads = poly
    _cities = cities


def land_rings():
    _load()
    return _land


def lake_rings():
    _load()
    return _lakes


def border_lines():
    _load()
    return _borders


def river_lines():
    _load()
    return _rivers


def road_lines():
    _load()
    return _roads


def cities():
    _load()
    return _cities
