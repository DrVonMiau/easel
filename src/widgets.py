"""Small reusable widgets: the striped placeholder swatch used for photo
thumbnails and album covers, and a scaled-thumbnail loader.

The stripes are drawn with GTK4's native Gtk.Snapshot/GSK API rather than
Cairo, so this doesn't pull in a pycairo dependency that may not be present
in the Flatpak runtime.
"""
import math
import os
import queue
import sys
import threading
from collections import OrderedDict

from gi.repository import Gdk, GdkPixbuf, GLib, Graphene, Gsk, Gtk

# Report the first few image-load failures to stderr, with the reason, so a
# problem that only shows up in the packaged runtime (a permission error vs a
# decode error) is diagnosable from the run console instead of guesswork.
_LOAD_FAIL_LOGGED = 0
_LOAD_FAIL_LOG_MAX = 8


def _log_load_failure(path, exc):
    global _LOAD_FAIL_LOGGED
    if _LOAD_FAIL_LOGGED >= _LOAD_FAIL_LOG_MAX:
        return
    _LOAD_FAIL_LOGGED += 1
    print(f"easel: could not load image {path!r}: {type(exc).__name__}: {exc}",
          file=sys.stderr)

STRIPE_STEP = 7
STRIPE_WIDTH = 2.4

# Loading a photo grid means many thumbnails at once. Two things must stay
# bounded or a large library takes the whole machine down:
#   * memory — decoding each file at full resolution (a 12MP photo is ~48 MB
#     decoded) and keeping it would exhaust RAM, so thumbnails are decoded to a
#     small size and the decoded textures live in a bounded LRU cache.
#   * concurrency — in the GNOME runtime each decode spawns a sandboxed glycin
#     subprocess; doing thousands at once, or synchronously on the main thread,
#     freezes the UI and floods the system. So decoding runs on a small fixed
#     pool of worker threads, off the main thread.
# Keyed by (path, size, mtime) so edits and size changes reload.
_THUMB_CACHE = OrderedDict()
_THUMB_CACHE_MAX = 320
_THUMB_CACHE_LOCK = threading.Lock()
# Cap the decoded dimension regardless of requested swatch size (retina
# headroom without unbounded memory).
_THUMB_MAX_DIM = 512

_LOADER_WORKERS = 3
# LIFO: the most recently requested tiles (usually the ones just scrolled into
# view) are decoded first.
_load_queue = queue.LifoQueue()
_workers_started = False
_workers_lock = threading.Lock()


def _cache_get(key):
    with _THUMB_CACHE_LOCK:
        texture = _THUMB_CACHE.get(key)
        if texture is not None:
            _THUMB_CACHE.move_to_end(key)
        return texture


def _cache_put(key, texture):
    with _THUMB_CACHE_LOCK:
        _THUMB_CACHE[key] = texture
        while len(_THUMB_CACHE) > _THUMB_CACHE_MAX:
            _THUMB_CACHE.popitem(last=False)


def _decode_scaled(path, size):
    dim = min(max(int(size) * 2, int(size)), _THUMB_MAX_DIM)
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, dim, dim, True)
        return _texture_from_pixbuf(pixbuf)
    except Exception as exc:
        _log_load_failure(path, exc)
        return None


def _loader_worker():
    while True:
        path, size, key, wants, callback = _load_queue.get()
        try:
            # Skip work the caller no longer wants (tile recycled / scrolled
            # away) so a big queue never forces thousands of pointless decodes.
            if wants is not None and not wants():
                continue
            texture = _cache_get(key)
            if texture is None:
                texture = _decode_scaled(path, size)
                if texture is not None:
                    _cache_put(key, texture)
            GLib.idle_add(callback, path, texture)
        except Exception:
            GLib.idle_add(callback, path, None)
        finally:
            _load_queue.task_done()


def _ensure_workers():
    global _workers_started
    with _workers_lock:
        if _workers_started:
            return
        _workers_started = True
        for _ in range(_LOADER_WORKERS):
            threading.Thread(target=_loader_worker, daemon=True).start()


def request_thumbnail(path, size, wants, callback):
    """Get a scaled thumbnail texture for `path`. Returns it immediately if
    cached; otherwise returns None and schedules a bounded background decode,
    calling callback(path, texture) on the main thread when ready (texture is
    None if the file can't be decoded). `wants()` is checked right before
    decoding so a recycled tile's stale request costs nothing."""
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    key = (path, size, mtime)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    _ensure_workers()
    _load_queue.put((path, size, key, wants, callback))
    return None


def _texture_from_pixbuf(pixbuf):
    """A Gdk.MemoryTexture copied from a pixbuf's pixels. We copy the bytes so
    the texture owns its data (no dependence on the pixbuf's lifetime), and we
    avoid the deprecated Gdk.Texture.new_for_pixbuf path."""
    if not pixbuf.get_has_alpha():
        pixbuf = pixbuf.add_alpha(False, 0, 0, 0)
    data = GLib.Bytes.new(pixbuf.get_pixels())
    return Gdk.MemoryTexture.new(
        pixbuf.get_width(), pixbuf.get_height(),
        Gdk.MemoryFormat.R8G8B8A8, data, pixbuf.get_rowstride())


def load_full_texture(path):
    """A full-resolution Gdk.Texture for `path` (the lightbox), or None if it
    can't be loaded. Tries GTK's own loaders first (they cover PNG/JPEG without
    needing gdk-pixbuf loader modules), then falls back to gdk-pixbuf."""
    if not path:
        return None
    try:
        return Gdk.Texture.new_from_filename(path)
    except Exception:
        pass
    try:
        return _texture_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file(path))
    except Exception as exc:
        _log_load_failure(path, exc)
        return None


def load_thumbnail(path, size):
    """A Gdk.Texture holding `path` decoded down to roughly `size` px, or None
    if the file can't be loaded (missing, corrupt, or a format with no loader
    such as HEIC) — callers then show the striped placeholder.

    Scaled decoding keeps memory bounded (a full 12MP photo is ~48 MB decoded);
    if the scaling loader is unavailable we fall back to a full-size texture so
    the image still shows rather than vanishing."""
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    key = (path, size, mtime)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    texture = _decode_scaled(path, size)
    if texture is None:
        texture = load_full_texture(path)
    if texture is not None:
        _cache_put(key, texture)
    return texture


# ---------- image adjustments (the editor) ----------

# Rec.709 luma weights for the saturation matrix.
_LUMA = (0.2126, 0.7152, 0.0722)


def _adjust_color_matrix(brightness, contrast, saturation):
    """Build the GSK colour matrix + offset for brightness/contrast/saturation.

    brightness: additive, 0 = none. contrast/saturation: multiplicative factors,
    1.0 = none. Composed as: out = k·(S·in) + (0.5·(1−k) + brightness), where S
    is the saturation matrix and k the contrast factor.

    GSK reads the 16 floats column-major (verified against the runtime), i.e.
    floats[col*4 + row], so we assemble a row-major matrix and transpose."""
    lr, lg, lb = _LUMA
    s = saturation
    sat = (
        (lr * (1 - s) + s, lg * (1 - s),     lb * (1 - s)),
        (lr * (1 - s),     lg * (1 - s) + s, lb * (1 - s)),
        (lr * (1 - s),     lg * (1 - s),     lb * (1 - s) + s),
    )
    k = contrast
    rows = [[k * sat[i][j] for j in range(3)] + [0.0] for i in range(3)]
    rows.append([0.0, 0.0, 0.0, 1.0])  # alpha untouched
    floats = [rows[r][c] for c in range(4) for r in range(4)]  # column-major
    matrix = Graphene.Matrix()
    matrix.init_from_float(floats)
    o = 0.5 * (1 - k) + brightness
    offset = Graphene.Vec4()
    offset.init(o, o, o, 0.0)
    return matrix, offset


def _snapshot_adjusted(snapshot, texture, adj, out_w, out_h):
    """Paint `texture` into `snapshot` at out_w×out_h with the adjustments in
    `adj` (a dict of brightness/contrast/saturation/rotation) applied. Shared by
    the live editor widget and the save renderer so preview and output match."""
    tw, th = texture.get_width(), texture.get_height()
    rot = adj["rotation"] % 360
    matrix, offset = _adjust_color_matrix(
        adj["brightness"], adj["contrast"], adj["saturation"])
    scale = min(out_w / (th if rot in (90, 270) else tw),
                out_h / (tw if rot in (90, 270) else th))
    snapshot.push_color_matrix(matrix, offset)
    snapshot.save()
    snapshot.translate(Graphene.Point().init(out_w / 2, out_h / 2))
    if rot:
        snapshot.rotate(rot)
    rw, rh = tw * scale, th * scale
    snapshot.append_texture(texture, Graphene.Rect().init(-rw / 2, -rh / 2, rw, rh))
    snapshot.restore()
    snapshot.pop()


def render_adjusted_texture(texture, adj):
    """Render `texture` with `adj` applied, at full resolution, to a new
    Gdk.Texture (used to save an edited copy). Returns None on failure."""
    if texture is None:
        return None
    tw, th = texture.get_width(), texture.get_height()
    rot = adj["rotation"] % 360
    out_w, out_h = (th, tw) if rot in (90, 270) else (tw, th)
    snapshot = Gtk.Snapshot()
    _snapshot_adjusted(snapshot, texture, adj, out_w, out_h)
    node = snapshot.to_node()
    if node is None:
        return None
    renderer = Gsk.CairoRenderer()
    try:
        renderer.realize(None)
        return renderer.render_texture(node, Graphene.Rect().init(0, 0, out_w, out_h))
    except Exception:
        return None
    finally:
        if renderer.is_realized():
            renderer.unrealize()


DEFAULT_ADJUSTMENTS = {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0,
                       "rotation": 0}


class AdjustableImage(Gtk.Widget):
    """Shows a texture with live brightness/contrast/saturation/rotation applied
    via a GSK colour matrix — the editor's canvas. Adjustments only change how
    it's drawn; the original pixels are never touched until the user saves."""

    __gtype_name__ = "EaselAdjustableImage"

    def __init__(self):
        super().__init__()
        self._texture = None
        self._adj = dict(DEFAULT_ADJUSTMENTS)
        self.set_hexpand(True)
        self.set_vexpand(True)

    def set_texture(self, texture):
        self._texture = texture
        self.queue_draw()

    def reset(self):
        self._adj = dict(DEFAULT_ADJUSTMENTS)
        self.queue_draw()

    def set_adjustment(self, name, value):
        self._adj[name] = value
        self.queue_draw()

    def rotate(self, degrees):
        self._adj["rotation"] = (self._adj["rotation"] + degrees) % 360
        self.queue_draw()

    def adjustments(self):
        return dict(self._adj)

    def do_measure(self, orientation, for_size):
        return (0, 320, -1, -1)

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        if self._texture is None or width <= 0 or height <= 0:
            return
        _snapshot_adjusted(snapshot, self._texture, self._adj, width, height)


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
        # Track the current request so an async result that arrives after the
        # swatch has been recycled to a different photo is ignored.
        self._req_path = path
        cached = request_thumbnail(
            path, self._size,
            wants=lambda p=path: getattr(self, "_req_path", None) == p,
            callback=self._on_thumb_ready)
        if cached is not None:
            self._apply_texture(cached)
        else:
            # Show the placeholder immediately; the image fills in when decoded.
            self._apply_texture(None)

    def _on_thumb_ready(self, path, texture):
        # Drop stale results for a since-recycled swatch.
        if getattr(self, "_req_path", None) != path:
            return False
        self._apply_texture(texture)
        return False

    def _apply_texture(self, texture):
        has_image = texture is not None
        # Always set the paintable (None clears it cleanly) so a recycled
        # swatch never briefly shows the previous photo or keeps stale content.
        self._picture.set_paintable(texture)
        self._picture.set_visible(has_image)
        self._area.set_visible(not has_image)
        self._label.set_visible(not has_image and bool(self._placeholder_text))
        self.queue_allocate()
