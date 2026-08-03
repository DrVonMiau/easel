"""Offline vector world map data for the Map view.

A compact set of Natural Earth 1:10m geometry — land coastlines, lakes and
country boundary lines — so the Map view can draw a detailed, resolution-
independent world with no map tiles, no network and no image decoding (which
is what fails in the sandbox). The geometry is simplified (~1/1000 degree) and
quantised to 1/100 degree, packed and zlib-compressed into worldmap.bin
(~350 KB) next to this module, and decoded lazily on first use.

Three accessors return lists of point sequences in (lon, lat) degrees:
  land_rings()   — filled land (islands/lakes are separate rings; even-odd)
  lake_rings()   — major lakes, drawn back in the sea colour
  border_lines() — country boundaries, stroked (open polylines, not closed)
"""
import os
import struct
import zlib

_BIN = os.path.join(os.path.dirname(__file__), "worldmap.bin")

_land = None
_lakes = None
_borders = None


def _parse_section(data, off):
    """Read a section: uint32 count, then each item = uint16 point count +
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


def _load():
    global _land, _lakes, _borders
    if _land is not None:
        return
    try:
        with open(_BIN, "rb") as fh:
            raw = zlib.decompress(fh.read())
    except (OSError, zlib.error):
        _land = _lakes = _borders = []
        return
    off = 0
    sections = []
    for _ in range(3):
        # Each section is length-prefixed (uint32) so the file is self-describing.
        (slen,) = struct.unpack_from("<I", raw, off)
        off += 4
        items, _end = _parse_section(raw, off)
        off += slen
        sections.append(items)
    _land, _lakes, _borders = sections


def land_rings():
    _load()
    return _land


def lake_rings():
    _load()
    return _lakes


def border_lines():
    _load()
    return _borders
