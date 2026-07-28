"""Offline world-map data for the Map view.

A coarse land/sea mask of the whole planet, so the Map view can draw a
recognisable world without any map tiles, network access or extra dependency —
Easel is offline-first and the sandbox can\'t reach tile servers.

The mask is a 720x360 (0.5 degree) equirectangular bitmap: one bit per cell,
row-major from the north-west corner, set where there is land. It is derived
from Natural Earth\'s public-domain 1:110m land polygons, rasterised once and
stored here zlib-compressed + base64 so the whole planet costs a few kilobytes
and decodes in a blink on first use.

`is_land(lon, lat)` is the only entry point the widget needs; the decode is
lazy and cached, so importing this module is cheap.
"""
import base64
import zlib

WIDTH = 720
HEIGHT = 360

# zlib(base64) of the packed 720x360 land bitmap (MSB-first, row-major).
_PACKED = (
    "eNrtnc1vHbt1wDmaW1EPFUQHb6OkiqiHFsiy7c4FFFHF+wOyzdJBFt10oSIooIe6njH0UjdIUXfXTRDnr8iyHkNAlEURJ7ss"
    "HioqLqAECOoRXMD085gsOR/3DmcOP2YkoQggLmTp6uo3Zw7POTw8PHeM0N24G3fjbtz4YEoVN4jDSilRf5fp79QNgqlq0cx8"
    "o+RNgWuuUu9USRsyvylyA1ZMdePNDUnNGpm56g/PHUaDEwWM5W8/b+d19f6sjCWnSj3XrKs+uFPGJ+23uOy9vZooc4F75BZE"
    "Vt/2DT6PM7i8sTRRY7rBl2a+BrjSfowmBJYHRGUDkTupUkBkPYVZhFWmHCt2UmmWBV7eL6seQeoLk/+aaL/7vkiMMjKIjEU2"
    "DiJrKA/PXWLkLrWr/LNN7nCUb0AWLcJxCG0h9FY9NTJTNbYNlPTvmxRLHYVclF1JuseEujRGSyBtQNbG8NJ4sEvhi1S9Fkz8"
    "qTqputDsJtNWRY0Eon3NhdbvEunV+86bfeSk1VDaV1emXroc0tjGm3cdCnviUaJWa8TS+7F6XMIBQ5Ba1KTc6MujoPUKC9RT"
    "Rih0mEvvJ1V2IT4u7agHqJmvFrOanRr6sTPIlYkiH9STV+9VfwmA4j7Tt3GvR5apvrFEucz5pdqstB0TJZUlEaA88wb2M8uf"
    "9LVyl2U8V7LC8oh2U9j93S/H92dug51RZgXEXsQqbLImyUQem1m56muDw6aRPbXD1uOPOsNGqP437fhaDVqlfydJZ8LfALRx"
    "mDfkQmvvuUW+aqZkb+WNmWrDzKFRg/4j0s1aGzs+9CNOexlaavJTaMmsLRB3Fim0IB+9qA1YfkWddH7HxjKzlaGkesbh1XgD"
    "rXxYavlePTQOVR1k+mLn9VSzkXOnq59Uat4FDPKY9E2raLIiLZ969t+kSuppW8+GHkZXHpmdOchKnakqR3YcPlMfyq/S16RK"
    "m5vOhqbRKEcvs9sLVrFL5RyiH9DMz9qTWKaISM+MctOhMhp/l4lAKjSGKZGov9IKnxqh8dC12+iXV82/rzxkjsCrV+mpuSpZ"
    "BfV+ZFMXZfPvSw/5PE+eAS8bY9VI3JlcYue/vHX7zKcOzoAX3xRGurIRUVtC+Wk7iTWqhD3EHs9rQxuN3xvpCG9msJ4K3pmG"
    "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"
)

_mask = None


def _ensure():
    global _mask
    if _mask is None:
        _mask = zlib.decompress(base64.b64decode("".join(_PACKED)))
    return _mask


def is_land(lon, lat):
    """True if the given longitude/latitude falls on land in the coarse mask.

    Longitude is clamped to [-180, 180) and latitude to [-90, 90]; anything out
    of range simply maps to the nearest edge cell, which is fine for the map\'s
    decorative dotted backdrop."""
    if lon < -180.0:
        lon = -180.0
    elif lon >= 180.0:
        lon = 179.999
    if lat > 90.0:
        lat = 90.0
    elif lat < -90.0:
        lat = -90.0
    x = int((lon + 180.0) / 360.0 * WIDTH)
    y = int((90.0 - lat) / 180.0 * HEIGHT)
    if x < 0:
        x = 0
    elif x >= WIDTH:
        x = WIDTH - 1
    if y < 0:
        y = 0
    elif y >= HEIGHT:
        y = HEIGHT - 1
    idx = y * WIDTH + x
    mask = _ensure()
    return bool(mask[idx >> 3] & (0x80 >> (idx & 7)))
