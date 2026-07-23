"""Small reusable widgets: the striped placeholder swatch used for photo
thumbnails and album covers, and a scaled-thumbnail loader.

The stripes are drawn with GTK4's native Gtk.Snapshot/GSK API rather than
Cairo, so this doesn't pull in a pycairo dependency that may not be present
in the Flatpak runtime.
"""
import math
import os
import sys
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
#   * work — doing every decode at once, or blocking the UI on each, freezes
#     the app.
# Decoding runs ON THE MAIN THREAD, a couple per idle cycle: in the GNOME
# runtime image decoding goes through glycin subprocesses that only work from
# the main thread (a background thread just yields blank images), so we can't
# use a worker pool. Idle-batching keeps it non-blocking and bounded instead.
# Keyed by (path, size, rotation, mtime) so edits/size/rotation changes reload.
_THUMB_CACHE = OrderedDict()
_THUMB_CACHE_MAX = 320
# Cap the decoded dimension regardless of requested swatch size (retina
# headroom without unbounded memory).
_THUMB_MAX_DIM = 512

# LIFO stack: the most recently requested tiles (usually the ones just scrolled
# into view) decode first. Processed on the main thread via an idle handler.
_load_stack = []
_load_idle_id = 0
_LOAD_BATCH = 2  # decodes per idle cycle — small, so the UI stays responsive

_ROTATIONS = {
    90: GdkPixbuf.PixbufRotation.CLOCKWISE,
    180: GdkPixbuf.PixbufRotation.UPSIDEDOWN,
    270: GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE,
}


def _cache_get(key):
    texture = _THUMB_CACHE.get(key)
    if texture is not None:
        _THUMB_CACHE.move_to_end(key)
    return texture


def _cache_put(key, texture):
    _THUMB_CACHE[key] = texture
    while len(_THUMB_CACHE) > _THUMB_CACHE_MAX:
        _THUMB_CACHE.popitem(last=False)


def _decode_scaled(path, size, rotation=0):
    dim = min(max(int(size) * 2, int(size)), _THUMB_MAX_DIM)
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, dim, dim, True)
        rot = _ROTATIONS.get(rotation % 360)
        if rot is not None:
            pixbuf = pixbuf.rotate_simple(rot)
        return _texture_from_pixbuf(pixbuf)
    except Exception as exc:
        _log_load_failure(path, exc)
        return None


def _process_load_stack():
    global _load_idle_id
    processed = 0
    while _load_stack and processed < _LOAD_BATCH:
        path, size, rotation, key, wants, callback = _load_stack.pop()  # LIFO
        # Skip work the caller no longer wants (tile recycled / scrolled away)
        # so a big backlog never forces thousands of pointless decodes.
        if wants is not None and not wants():
            continue
        texture = _cache_get(key)
        if texture is None:
            texture = _decode_scaled(path, size, rotation)
            if texture is not None:
                _cache_put(key, texture)
        callback(path, texture)
        processed += 1
    if _load_stack:
        return True  # keep the idle handler running
    _load_idle_id = 0
    return False


def request_thumbnail(path, size, wants, callback, rotation=0):
    """Get a scaled thumbnail texture for `path`. Returns it immediately if
    cached; otherwise returns None and schedules a bounded main-thread decode,
    calling callback(path, texture) when ready (texture is None if the file
    can't be decoded). `wants()` is checked right before decoding so a recycled
    tile's stale request costs nothing."""
    global _load_idle_id
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    key = (path, size, rotation, mtime)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    _load_stack.append((path, size, rotation, key, wants, callback))
    if not _load_idle_id:
        _load_idle_id = GLib.idle_add(_process_load_stack)
    return None


def _texture_from_pixbuf(pixbuf):
    """A Gdk.Texture from a pixbuf. Uses Gdk.Texture.new_for_pixbuf: although
    deprecated, it renders reliably in the GNOME runtime, whereas a hand-built
    Gdk.MemoryTexture there stayed invisible (thumbnails/preview/rotated views
    all blank while the plain new_from_filename path worked)."""
    return Gdk.Texture.new_for_pixbuf(pixbuf)


def load_full_texture(path, rotation=0):
    """A full-resolution Gdk.Texture for `path` (the lightbox), or None if it
    can't be loaded, with an optional non-destructive rotation applied. Tries
    GTK's own loaders first (they cover PNG/JPEG without needing gdk-pixbuf
    loader modules) when unrotated, then falls back to gdk-pixbuf."""
    if not path:
        return None
    rot = _ROTATIONS.get(rotation % 360)
    if rot is None:
        try:
            return Gdk.Texture.new_from_filename(path)
        except Exception:
            pass
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
        if rot is not None:
            pixbuf = pixbuf.rotate_simple(rot)
        return _texture_from_pixbuf(pixbuf)
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


class Swatch(Gtk.Widget):
    """A square artwork swatch: draws the thumbnail texture (cover-cropped) when
    a path is set, otherwise a diagonal-striped placeholder.

    The texture is painted directly in do_snapshot rather than via a child
    Gtk.Picture: in the GNOME runtime a Gtk.Picture child inside a custom widget
    doesn't paint, whereas self-drawn content (like the editor canvas) does.
    Overflow-hidden + the .swatch CSS border-radius round the corners."""

    __gtype_name__ = "EaselSwatch"

    def __init__(self, placeholder_text="", size=128):
        super().__init__()
        self._size = size
        self._texture = None
        self._placeholder_text = placeholder_text  # kept for API compatibility
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.add_css_class("swatch")
        self.set_path(None)

    def do_measure(self, orientation, for_size):
        return (self._size, self._size, -1, -1)

    def set_size(self, size):
        if size != self._size:
            self._size = size
            self.queue_resize()

    def set_placeholder(self, text):
        self._placeholder_text = text

    def set_path(self, path, rotation=0):
        # Track the current request (path + rotation) so a result that arrives
        # after the swatch has been recycled or re-rotated is ignored.
        token = (path, rotation)
        self._req_token = token

        def on_ready(_path, texture, want=token):
            if getattr(self, "_req_token", None) == want:
                self._set_texture(texture)
            return False

        cached = request_thumbnail(
            path, self._size,
            wants=lambda want=token: getattr(self, "_req_token", None) == want,
            callback=on_ready, rotation=rotation)
        # Cached hit paints now; otherwise show the placeholder until it decodes.
        self._set_texture(cached)

    def _set_texture(self, texture):
        self._texture = texture
        self.queue_draw()

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        if self._texture is not None:
            tw, th = self._texture.get_width(), self._texture.get_height()
            if tw > 0 and th > 0:
                # content-fit: cover — scale to fill, centre-crop (overflow
                # hidden clips to the rounded corners).
                scale = max(width / tw, height / th)
                dw, dh = tw * scale, th * scale
                snapshot.append_texture(
                    self._texture,
                    Graphene.Rect().init((width - dw) / 2, (height - dh) / 2, dw, dh))
            return
        # Striped placeholder, drawn in the widget's CSS `color`.
        rgba = self.get_color()
        snapshot.save()
        snapshot.translate(Graphene.Point().init(width / 2, height / 2))
        snapshot.rotate(45)
        diag = math.hypot(width, height)
        y = -diag
        while y < diag:
            snapshot.append_color(rgba, Graphene.Rect().init(-diag, y, diag * 2, STRIPE_WIDTH))
            y += STRIPE_STEP
        snapshot.restore()
