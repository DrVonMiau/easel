"""Small reusable widgets: the striped placeholder art swatch used for
artists/albums/tracks until real cover art or an artist photo is fetched.

The stripes are drawn with GTK4's native Gtk.Snapshot/GSK API rather than
Cairo, so this doesn't pull in a pycairo dependency that may not be present
in the Flatpak runtime.
"""
import math

from gi.repository import Graphene, Gsk, Gtk

STRIPE_STEP = 7
STRIPE_WIDTH = 2.4


class _StripeArea(Gtk.Widget):
    """Fills its allocated area with a 45-degree repeating stripe pattern.
    The stripe color is the widget's CSS `color`, so it follows the theme."""

    __gtype_name__ = "EaselStripeArea"

    def do_snapshot(self, snapshot):
        width = self.get_width()
        height = self.get_height()
        if width <= 0 or height <= 0:
            return
        rgba = self.get_color()
        snapshot.push_clip(Graphene.Rect().init(0, 0, width, height))
        snapshot.save()
        snapshot.translate(Graphene.Point().init(width / 2, height / 2))
        snapshot.rotate(45)
        diag = math.hypot(width, height)
        y = -diag
        while y < diag:
            stripe = Graphene.Rect().init(-diag, y, diag * 2, STRIPE_WIDTH)
            snapshot.append_color(rgba, stripe)
            y += STRIPE_STEP
        snapshot.restore()
        snapshot.pop()


class Swatch(Gtk.Widget):
    """A square artwork swatch: shows a Gtk.Picture when a path is set,
    otherwise a diagonal-striped placeholder with a small caption chip.

    Implemented as a plain widget with manual measure/allocate so it is
    always square, no matter the aspect ratio of the image inside (the
    picture crops via content-fit cover and is clipped to the corners).
    """

    __gtype_name__ = "EaselSwatch"

    def __init__(self, placeholder_text, size=128):
        super().__init__()
        self._size = size
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.add_css_class("swatch")
        self._placeholder_text = placeholder_text

        self._picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER)
        self._picture.set_parent(self)

        self._area = _StripeArea()
        self._area.set_parent(self)

        self._label = Gtk.Label(label=placeholder_text or "")
        self._label.add_css_class("swatch-caption")
        self._label.set_parent(self)

        self.set_path(None)
        # PyGObject doesn't reliably invoke do_dispose overrides, so unparent
        # the manually-parented children on ::destroy instead.
        self.connect("destroy", self._on_destroy)

    def _on_destroy(self, *_args):
        for child in (self._picture, self._area, self._label):
            if child.get_parent() is self:
                child.unparent()

    def do_measure(self, orientation, for_size):
        for child in (self._picture, self._area, self._label):
            child.measure(orientation, -1)
        return (self._size, self._size, -1, -1)

    def do_size_allocate(self, width, height, baseline):
        for child in (self._picture, self._area):
            if child.get_visible():
                child.allocate(width, height, -1, None)
        if self._label.get_visible():
            _lmin, lnat, _b1, _b2 = self._label.measure(Gtk.Orientation.HORIZONTAL, -1)
            label_w = min(lnat, width)
            _hmin, hnat, _b3, _b4 = self._label.measure(Gtk.Orientation.VERTICAL, label_w)
            transform = Gsk.Transform.new().translate(
                Graphene.Point().init((width - label_w) / 2, (height - hnat) / 2)
            )
            self._label.allocate(label_w, hnat, -1, transform)

    def set_size(self, size):
        if size != self._size:
            self._size = size
            self.queue_resize()

    def set_placeholder(self, text):
        self._placeholder_text = text
        self._label.set_label(text or "")

    def set_path(self, path):
        has_path = bool(path)
        self._picture.set_visible(has_path)
        if has_path:
            self._picture.set_filename(path)
        self._area.set_visible(not has_path)
        self._label.set_visible(not has_path and bool(self._placeholder_text))
        self.queue_allocate()
