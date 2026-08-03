"""A real, zoomable OpenStreetMap view built on libshumate, with photo
thumbnails as the markers (Apple Photos style).

libshumate is bundled in the Flatpak manifest (it isn't in the GNOME runtime),
so this needs network for the tiles. It is imported defensively by window.py:
if libshumate is somehow missing, Easel falls back to the offline vector map,
so the Map view is never blank.

The widget exposes the same tiny interface as the offline MapView
(`set_photos` / `set_activate_cb`). Photos taken at essentially the same spot
(~100 m) share one thumbnail pin that shows a count; clicking a pin opens all
of its photos in the lightbox.
"""
import gi

gi.require_version("Shumate", "1.0")
from gi.repository import Gtk, Shumate  # noqa: E402

from .widgets import Swatch  # noqa: E402

# The OSM "Mapnik" raster source id; fall back to the literal if the #define
# isn't exposed as a constant in the introspection bindings.
_OSM = getattr(Shumate, "MAP_SOURCE_OSM_MAPNIK", "osm-mapnik")


class ShumateMap(Gtk.Box):
    """A Shumate.SimpleMap (pan, scroll/pinch zoom, zoom buttons, OSM licence)
    with a photo-thumbnail marker per location."""

    __gtype_name__ = "EaselShumateMap"

    def __init__(self):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._activate_cb = None

        self._simple = Shumate.SimpleMap()
        self._simple.set_hexpand(True)
        self._simple.set_vexpand(True)

        registry = Shumate.MapSourceRegistry.new_with_defaults()
        source = registry.get_by_id(_OSM)
        if source is not None:
            self._simple.set_map_source(source)

        self._map = self._simple.get_map()
        self._viewport = self._simple.get_viewport()
        if source is not None:
            self._viewport.set_reference_map_source(source)
        self._viewport.set_min_zoom_level(2)
        self._viewport.set_max_zoom_level(18)
        self._viewport.set_zoom_level(3)

        self._marker_layer = Shumate.MarkerLayer.new(self._viewport)
        self._simple.add_overlay_layer(self._marker_layer)

        self.append(self._simple)

    def set_activate_cb(self, cb):
        self._activate_cb = cb

    def set_photos(self, entries):
        """entries: iterable of (lon, lat, photo)."""
        self._marker_layer.remove_all()
        # Group photos taken at essentially the same place (~110 m) so one spot
        # is one thumbnail.
        groups = {}
        for lon, lat, photo in entries:
            key = (round(lat, 3), round(lon, 3))
            groups.setdefault(key, []).append((lat, lon, photo))

        lats, lons = [], []
        for members in groups.values():
            mlat = sum(m[0] for m in members) / len(members)
            mlon = sum(m[1] for m in members) / len(members)
            photos = [m[2] for m in members]
            self._marker_layer.add_marker(self._make_marker(mlat, mlon, photos))
            lats.append(mlat)
            lons.append(mlon)
        if lats:
            self._frame(lats, lons)

    def _make_marker(self, lat, lon, photos):
        marker = Shumate.Marker()
        marker.set_location(lat, lon)
        marker.set_selectable(False)
        thumb = Swatch("", size=52)
        thumb.add_css_class("map-thumb")
        thumb.set_path(photos[0].path)
        overlay = Gtk.Overlay(css_classes=["map-marker"])
        overlay.set_child(thumb)
        n = len(photos)
        if n > 1:
            overlay.add_overlay(Gtk.Label(
                label=str(n) if n < 1000 else "999+",
                halign=Gtk.Align.END, valign=Gtk.Align.END,
                css_classes=["map-thumb-badge"]))
        marker.set_child(overlay)
        click = Gtk.GestureClick(button=1)
        click.connect("released", lambda *_a, p=photos:
                      self._activate_cb(p) if self._activate_cb else None)
        marker.add_controller(click)
        return marker

    def _frame(self, lats, lons):
        """Centre on the photos and pick a zoom that roughly fits their spread."""
        clat = (min(lats) + max(lats)) / 2.0
        clon = (min(lons) + max(lons)) / 2.0
        span = max(max(lats) - min(lats), (max(lons) - min(lons)) / 2.0)
        if len(lats) == 1 or span < 1e-4:
            z = 12
        elif span > 120:
            z = 2
        elif span > 60:
            z = 3
        elif span > 20:
            z = 4
        elif span > 5:
            z = 6
        elif span > 1:
            z = 9
        else:
            z = 11
        self._viewport.set_zoom_level(z)
        self._map.center_on(clat, clon)
