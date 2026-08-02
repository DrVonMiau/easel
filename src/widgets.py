"""Small reusable widgets: the striped placeholder swatch used for photo
thumbnails and album covers, and a scaled-thumbnail loader.

The stripes are drawn with GTK4's native Gtk.Snapshot/GSK API rather than
Cairo, so this doesn't pull in a pycairo dependency that may not be present
in the Flatpak runtime.
"""
import hashlib
import math
import os
import sys
from collections import OrderedDict

from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk

from . import worldmap

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


def _thumb_cache_dir():
    """The on-disk directory where baked thumbnail PNGs live. Under
    XDG_CACHE_HOME (writable in the Flatpak sandbox), created on demand."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "easel", "thumbs")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _thumb_cache_file(path, dim, rotation, mtime):
    digest = hashlib.sha1(
        f"{path}|{dim}|{rotation}|{mtime}".encode("utf-8", "surrogatepass")
    ).hexdigest()
    return os.path.join(_thumb_cache_dir(), digest + ".png")

# LIFO stack: the most recently requested tiles (usually the ones just scrolled
# into view) decode first. Processed on the main thread via an idle handler.
_load_stack = []
_load_idle_id = 0
_LOAD_BATCH = 2  # decodes per idle cycle — small, so the UI stays responsive

def _render_texture(texture, rotation=0, max_dim=None):
    """Return a new Gdk.Texture that is `texture` optionally rotated by a
    multiple of 90° and scaled so its longest side is at most `max_dim`
    (no upscaling; max_dim=None keeps full size).

    Done entirely with GSK — a snapshot rendered through Gsk.CairoRenderer —
    rather than gdk-pixbuf, because in the GNOME 49 runtime gdk-pixbuf decoding
    is delegated to a glycin subprocess that fails ('Loader process exited early
    with status 1'), while Gdk.Texture.new_from_filename works. So we decode the
    source with new_from_filename and reshape it here."""
    tw, th = texture.get_width(), texture.get_height()
    if tw <= 0 or th <= 0:
        return None
    scale = 1.0
    if max_dim:
        scale = min(max_dim / tw, max_dim / th, 1.0)
    sw, sh = max(1, round(tw * scale)), max(1, round(th * scale))
    rot = rotation % 360
    out_w, out_h = (sh, sw) if rot in (90, 270) else (sw, sh)
    snapshot = Gtk.Snapshot()
    snapshot.save()
    snapshot.translate(Graphene.Point().init(out_w / 2, out_h / 2))
    if rot:
        snapshot.rotate(rot)
    snapshot.append_scaled_texture(
        texture, Gsk.ScalingFilter.TRILINEAR,
        Graphene.Rect().init(-sw / 2, -sh / 2, sw, sh))
    snapshot.restore()
    node = snapshot.to_node()
    if node is None:
        return None
    renderer = Gsk.CairoRenderer()
    try:
        renderer.realize(None)
        return renderer.render_texture(
            node, Graphene.Rect().init(0, 0, out_w, out_h))
    except Exception:
        return None
    finally:
        if renderer.is_realized():
            renderer.unrealize()


def _texture_to_cache(texture, cache_file):
    """Write `texture` to `cache_file` (atomically) as PNG. Returns True on
    success. The bytes are produced with Gdk.Texture.save_to_png_bytes so no
    gdk-pixbuf save path is involved."""
    try:
        data = texture.save_to_png_bytes()
        tmp = f"{cache_file}.{os.getpid()}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data.get_data())
        os.replace(tmp, cache_file)  # atomic: a half-written file is never read
        return True
    except Exception:
        return False


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
    """Decode `path` scaled to ~`size` (with any rotation baked in) and return a
    renderable Gdk.Texture, or None if it can't be loaded.

    The source is decoded with Gdk.Texture.new_from_filename (the only decode
    path that works in the GNOME 49 runtime — gdk-pixbuf's glycin subprocess
    fails there), scaled/rotated via GSK, and the result is cached to a small
    PNG on disk. That PNG is then loaded back with new_from_filename so what the
    grid paints is always a plain file-backed texture."""
    dim = min(max(int(size) * 2, int(size)), _THUMB_MAX_DIM)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    cache_file = _thumb_cache_file(path, dim, rotation, mtime)
    if not os.path.exists(cache_file):
        try:
            src = Gdk.Texture.new_from_filename(path)
        except Exception as exc:
            _log_load_failure(path, exc)
            return None
        scaled = _render_texture(src, rotation, max_dim=dim)
        if scaled is None or not _texture_to_cache(scaled, cache_file):
            return None
    try:
        return Gdk.Texture.new_from_filename(cache_file)
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


def load_full_texture(path, rotation=0):
    """A full-resolution Gdk.Texture for `path` (the lightbox), or None if it
    can't be loaded, with an optional non-destructive rotation applied.

    Unrotated, the file loads straight through Gdk.Texture.new_from_filename.
    Rotated, the source texture is rotated via GSK and baked to a PNG in the
    cache, then loaded back with new_from_filename — a GSK-rendered texture is
    not guaranteed to paint on screen in this runtime, whereas a file-backed one
    always does."""
    if not path:
        return None
    try:
        src = Gdk.Texture.new_from_filename(path)
    except Exception as exc:
        _log_load_failure(path, exc)
        return None
    if rotation % 360 == 0:
        return src
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0
    cache_file = _thumb_cache_file(path, "full", rotation, mtime)
    if not os.path.exists(cache_file):
        rotated = _render_texture(src, rotation, max_dim=None)
        if rotated is None or not _texture_to_cache(rotated, cache_file):
            return None
    try:
        return Gdk.Texture.new_from_filename(cache_file)
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


def _adjust_color_matrix(brightness, contrast, saturation, exposure=1.0,
                         temperature=0.0, tone="none"):
    """Build the GSK colour matrix + offset for the editor's adjustments.

    brightness: additive, 0 = none. contrast/saturation/exposure: multiplicative
    factors, 1.0 = none. temperature: -1..1, warms (>0) or cools (<0) the image
    by scaling the red/blue channels. tone: "sepia" swaps the saturation matrix
    for a fixed sepia tint. Composed as:
        out = k·(T·g·B·in) + (0.5·(1−k) + brightness)
    where B is the base 3×3 (saturation or sepia), g the exposure gain, T the
    per-channel temperature scale and k the contrast factor.

    GSK reads the 16 floats column-major (verified against the runtime), i.e.
    floats[col*4 + row], so we assemble a row-major matrix and transpose."""
    lr, lg, lb = _LUMA
    if tone == "sepia":
        base = (
            (0.393, 0.769, 0.189),
            (0.349, 0.686, 0.168),
            (0.272, 0.534, 0.131),
        )
    else:
        s = saturation
        base = (
            (lr * (1 - s) + s, lg * (1 - s),     lb * (1 - s)),
            (lr * (1 - s),     lg * (1 - s) + s, lb * (1 - s)),
            (lr * (1 - s),     lg * (1 - s),     lb * (1 - s) + s),
        )
    k = contrast
    g = exposure
    # Warm (>0): lift red, drop blue; cool (<0): the reverse.
    t = (1.0 + 0.2 * temperature, 1.0, 1.0 - 0.2 * temperature)
    rows = [[k * g * t[i] * base[i][j] for j in range(3)] + [0.0] for i in range(3)]
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
        adj["brightness"], adj["contrast"], adj["saturation"],
        adj.get("exposure", 1.0), adj.get("temperature", 0.0),
        adj.get("tone", "none"))
    scale = min(out_w / (th if rot in (90, 270) else tw),
                out_h / (tw if rot in (90, 270) else th))
    snapshot.push_color_matrix(matrix, offset)
    snapshot.save()
    snapshot.translate(Graphene.Point().init(out_w / 2, out_h / 2))
    # Straighten: a fine rotation of the whole displayed image (screen space,
    # so it composes on top of the 90° steps and flips). Zoom up so the rotated
    # image still fills its frame — no empty corners to crop around.
    straighten = adj.get("straighten", 0.0)
    if straighten:
        rw0, rh0 = tw * scale, th * scale
        a = math.radians(straighten)
        ratio = max(rw0 / rh0, rh0 / rw0) if rh0 and rw0 else 1.0
        zoom = abs(math.cos(a)) + ratio * abs(math.sin(a))
        snapshot.scale(zoom, zoom)
        snapshot.rotate(straighten)
    if rot:
        snapshot.rotate(rot)
    # Mirror horizontally / vertically about the image centre.
    sx = -1.0 if adj.get("flip_h") else 1.0
    sy = -1.0 if adj.get("flip_v") else 1.0
    if sx != 1.0 or sy != 1.0:
        snapshot.scale(sx, sy)
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
    # A crop (normalised display-space x, y, w, h) selects the output sub-region:
    # render only that rectangle of the composed node, so the texture comes out
    # already cropped at full resolution.
    crop = adj.get("crop")
    if crop:
        cx, cy, cw, ch = crop
        vx, vy = cx * out_w, cy * out_h
        vw, vh = max(1.0, cw * out_w), max(1.0, ch * out_h)
    else:
        vx, vy, vw, vh = 0.0, 0.0, out_w, out_h
    renderer = Gsk.CairoRenderer()
    try:
        renderer.realize(None)
        return renderer.render_texture(node, Graphene.Rect().init(vx, vy, vw, vh))
    except Exception:
        return None
    finally:
        if renderer.is_realized():
            renderer.unrealize()


DEFAULT_ADJUSTMENTS = {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0,
                       "exposure": 1.0, "temperature": 0.0, "tone": "none",
                       "flip_h": False, "flip_v": False, "rotation": 0,
                       "straighten": 0.0}


class AdjustableImage(Gtk.Widget):
    """Shows a texture with live brightness/contrast/saturation/rotation applied
    via a GSK colour matrix — the editor's canvas. Adjustments only change how
    it's drawn; the original pixels are never touched until the user saves."""

    __gtype_name__ = "EaselAdjustableImage"

    def __init__(self):
        super().__init__()
        self._texture = None
        self._adj = dict(DEFAULT_ADJUSTMENTS)
        self._crop = None   # applied crop (x, y, w, h normalised), shown live
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)

    def set_texture(self, texture):
        self._texture = texture
        self.queue_draw()

    def set_crop(self, crop):
        """Show only this normalised (x, y, w, h) region of the image, fit to the
        widget — so an applied crop is visible while adjusting/filtering, not a
        surprise at save time. None shows the whole image."""
        self._crop = crop
        self.queue_draw()

    def reset(self):
        self._adj = dict(DEFAULT_ADJUSTMENTS)
        self._crop = None
        self.queue_draw()

    def set_adjustment(self, name, value):
        self._adj[name] = value
        self.queue_draw()

    def rotate(self, degrees):
        self._adj["rotation"] = (self._adj["rotation"] + degrees) % 360
        self.queue_draw()

    def toggle_flip(self, axis):
        key = "flip_h" if axis == "h" else "flip_v"
        self._adj[key] = not self._adj.get(key, False)
        self.queue_draw()

    def adjustments(self):
        return dict(self._adj)

    def do_measure(self, orientation, for_size):
        return (0, 320, -1, -1)

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        texture = self._texture
        if texture is None or width <= 0 or height <= 0:
            return
        if not self._crop:
            _snapshot_adjusted(snapshot, texture, self._adj, width, height)
            return
        # Cropped preview: draw the whole adjusted image at the on-screen size
        # its crop region needs to fill the widget, then offset so that region is
        # centred; overflow is clipped away. Drawing at the final on-screen size
        # (rather than native size + a scale()) keeps coordinates small and
        # avoids the renderer culling an off-viewport draw. Matches the region
        # render_adjusted_texture saves.
        tw, th = texture.get_width(), texture.get_height()
        rot = self._adj["rotation"] % 360
        disp_w, disp_h = (th, tw) if rot in (90, 270) else (tw, th)
        cx, cy, cw, ch = self._crop
        cw = max(cw, 1e-3)
        ch = max(ch, 1e-3)
        # Scale so the crop region exactly fits the widget (contain).
        s = min(width / (cw * disp_w), height / (ch * disp_h))
        full_w, full_h = disp_w * s, disp_h * s      # whole image, on screen
        crop_w_px, crop_h_px = cw * full_w, ch * full_h
        crop_left = (width - crop_w_px) / 2.0
        crop_top = (height - crop_h_px) / 2.0
        off_x = crop_left - cx * full_w
        off_y = crop_top - cy * full_h
        # Clip to the crop rectangle itself, so the rest of the image (which
        # bleeds into the letter/pillar-box margins) is hidden — otherwise the
        # preview looks uncropped.
        snapshot.push_clip(Graphene.Rect().init(crop_left, crop_top, crop_w_px, crop_h_px))
        snapshot.save()
        snapshot.translate(Graphene.Point().init(off_x, off_y))
        _snapshot_adjusted(snapshot, texture, self._adj, full_w, full_h)
        snapshot.restore()
        snapshot.pop()


class FilterThumb(Gtk.Widget):
    """A small square preview of a texture with one set of colour adjustments
    baked in — used for the editor's filter chips so each filter shows what it
    does to the current photo rather than a bare label.

    Only the colour matrix is applied (filters never rotate/flip), and the image
    is cover-cropped into the square; overflow-hidden + CSS radius round it."""

    __gtype_name__ = "EaselFilterThumb"

    def __init__(self, size=54):
        super().__init__()
        self._size = size
        self._texture = None
        self._adj = dict(DEFAULT_ADJUSTMENTS)
        self.set_overflow(Gtk.Overflow.HIDDEN)

    def do_measure(self, orientation, for_size):
        return (self._size, self._size, -1, -1)

    def set_source(self, texture, adj):
        self._texture = texture
        self._adj = dict(adj)
        self.queue_draw()

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        if self._texture is None or width <= 0 or height <= 0:
            return
        tw, th = self._texture.get_width(), self._texture.get_height()
        if tw <= 0 or th <= 0:
            return
        matrix, offset = _adjust_color_matrix(
            self._adj["brightness"], self._adj["contrast"], self._adj["saturation"],
            self._adj.get("exposure", 1.0), self._adj.get("temperature", 0.0),
            self._adj.get("tone", "none"))
        # content-fit: cover — scale to fill the square, centre-crop.
        scale = max(width / tw, height / th)
        dw, dh = tw * scale, th * scale
        snapshot.push_color_matrix(matrix, offset)
        snapshot.append_texture(
            self._texture,
            Graphene.Rect().init((width - dw) / 2, (height - dh) / 2, dw, dh))
        snapshot.pop()


class AdjustScale(Gtk.Scale):
    """A GtkScale that paints a small dot at its centre (the neutral/original
    value) so how far an adjustment has moved is obvious without a reset button.

    The dot is drawn after the base scale, so it sits on top of the blue fill as
    well as the grey track. It's suppressed while the handle rests at the centre
    (the handle already marks the origin then, and a dot over it looks odd)."""

    __gtype_name__ = "EaselAdjustScale"

    _DOT_RADIUS = 2.0

    def do_snapshot(self, snapshot):
        Gtk.Scale.do_snapshot(self, snapshot)
        adj = self.get_adjustment()
        if adj is None:
            return
        lo, hi = adj.get_lower(), adj.get_upper()
        span = hi - lo
        if span <= 0:
            return
        mid = (lo + hi) / 2.0
        # Hidden while the handle is (near) the centre — it covers the spot.
        if abs(adj.get_value() - mid) / span < 0.03:
            return
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        r = self._DOT_RADIUS
        rect = Graphene.Rect().init(width / 2.0 - r, height / 2.0 - r, 2 * r, 2 * r)
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(rect, r)
        white = Gdk.RGBA()
        white.red = white.green = white.blue = white.alpha = 1.0
        snapshot.push_rounded_clip(rounded)
        snapshot.append_color(white, rect)
        snapshot.pop()


class CropOverlay(Gtk.Widget):
    """Interactive crop rectangle drawn over the editor image.

    Coordinates are stored normalised (0..1) in *display* space — the image as
    shown, after rotation/flip — so the same rect maps straight onto the saved
    render (render_adjusted_texture crops the display-space output by it). The
    widget shares the image's allocation (it's an overlay child) and fits the
    display aspect the same contain way the canvas does, so the frame lines up.

    It only draws / takes input while active (crop mode). The crop is applied to
    the exported image on save, not to the live canvas."""

    __gtype_name__ = "EaselCropOverlay"

    _HANDLE = 16.0   # px hit radius around a handle
    _MIN = 0.06      # smallest normalised crop extent

    def __init__(self):
        super().__init__()
        self._disp_w = 1.0
        self._disp_h = 1.0
        self._active = False
        self._crop = [0.0, 0.0, 1.0, 1.0]  # x0, y0, x1, y1
        self._aspect = None   # None = free; else locked normalised width/height
        self._drag_handle = None
        self._drag_start = None
        self._pointer = Gdk.Cursor.new_from_name("pointer", None)
        self.set_visible(False)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)
        # Show the hand cursor only over a grabbable spot (handle or the crop
        # interior), so an empty area doesn't imply you can draw a box there.
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)

    def do_measure(self, orientation, for_size):
        return (0, 0, -1, -1)

    def set_display_size(self, w, h):
        self._disp_w = max(1.0, float(w))
        self._disp_h = max(1.0, float(h))
        if self._aspect is not None:
            self._snap_to_aspect()
        self.queue_draw()

    def set_crop(self, x0, y0, x1, y1):
        self._crop = [x0, y0, x1, y1]
        self.queue_draw()

    def set_aspect_ratio(self, ratio, snap=True):
        """Lock the crop to a normalised width/height ratio (None = free). When
        locking, snap the current rect to a centred rect of that shape — unless
        snap is False (used when restoring an exact rect on cancel)."""
        self._aspect = ratio
        if ratio is not None and snap:
            self._snap_to_aspect()
        self.queue_draw()

    def _snap_to_aspect(self):
        r = self._aspect
        if not r or r <= 0:
            return
        cw, ch = 1.0, 1.0 / r
        if ch > 1.0:
            cw, ch = r, 1.0
        x0 = (1.0 - cw) / 2.0
        y0 = (1.0 - ch) / 2.0
        self._crop = [x0, y0, x0 + cw, y0 + ch]

    def set_active(self, active):
        self._active = bool(active)
        self.set_visible(self._active)
        self.queue_draw()

    def is_active(self):
        return self._active

    def reset(self):
        self._crop = [0.0, 0.0, 1.0, 1.0]
        self.queue_draw()

    def get_crop(self):
        """Normalised (x, y, w, h) in display space, or None when it's the whole
        image (nothing to crop)."""
        x0, y0, x1, y1 = self._crop
        if x0 <= 0.002 and y0 <= 0.002 and x1 >= 0.998 and y1 >= 0.998:
            return None
        return (x0, y0, x1 - x0, y1 - y0)

    def _image_rect(self):
        """The pixel rect the image occupies in this widget (contain-fit,
        centred) — matches how AdjustableImage places it."""
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return (0.0, 0.0, 0.0, 0.0)
        scale = min(width / self._disp_w, height / self._disp_h)
        iw, ih = self._disp_w * scale, self._disp_h * scale
        return ((width - iw) / 2.0, (height - ih) / 2.0, iw, ih)

    def _crop_px(self):
        ix, iy, iw, ih = self._image_rect()
        x0, y0, x1, y1 = self._crop
        return (ix + x0 * iw, iy + y0 * ih, ix + x1 * iw, iy + y1 * ih)

    @staticmethod
    def _fill(snapshot, rgba, x, y, w, h):
        if w > 0 and h > 0:
            snapshot.append_color(rgba, Graphene.Rect().init(x, y, w, h))

    def do_snapshot(self, snapshot):
        if not self._active:
            return
        ix, iy, iw, ih = self._image_rect()
        if iw <= 0 or ih <= 0:
            return
        cx0, cy0, cx1, cy1 = self._crop_px()
        dim = Gdk.RGBA()
        dim.red = dim.green = dim.blue = 0.0
        dim.alpha = 0.5
        # Dim the image outside the crop (four bands).
        self._fill(snapshot, dim, ix, iy, iw, cy0 - iy)
        self._fill(snapshot, dim, ix, cy1, iw, iy + ih - cy1)
        self._fill(snapshot, dim, ix, cy0, cx0 - ix, cy1 - cy0)
        self._fill(snapshot, dim, cx1, cy0, ix + iw - cx1, cy1 - cy0)
        white = Gdk.RGBA()
        white.red = white.green = white.blue = white.alpha = 1.0
        # Rule-of-thirds guides.
        guide = Gdk.RGBA()
        guide.red = guide.green = guide.blue = 1.0
        guide.alpha = 0.35
        for t in (1 / 3.0, 2 / 3.0):
            self._fill(snapshot, guide, cx0 + (cx1 - cx0) * t - 0.5, cy0, 1.0, cy1 - cy0)
            self._fill(snapshot, guide, cx0, cy0 + (cy1 - cy0) * t - 0.5, cx1 - cx0, 1.0)
        # Border.
        t = 2.0
        self._fill(snapshot, white, cx0, cy0, cx1 - cx0, t)
        self._fill(snapshot, white, cx0, cy1 - t, cx1 - cx0, t)
        self._fill(snapshot, white, cx0, cy0, t, cy1 - cy0)
        self._fill(snapshot, white, cx1 - t, cy0, t, cy1 - cy0)
        # Handles (corners + edge midpoints).
        s = 4.0
        for hx, hy in self._handle_points(cx0, cy0, cx1, cy1).values():
            self._fill(snapshot, white, hx - s, hy - s, 2 * s, 2 * s)

    def _handle_points(self, cx0, cy0, cx1, cy1):
        pts = {"nw": (cx0, cy0), "ne": (cx1, cy0),
               "sw": (cx0, cy1), "se": (cx1, cy1)}
        # Edge handles only make sense when the aspect is free (they'd otherwise
        # change one dimension independently and break the lock).
        if self._aspect is None:
            mx, my = (cx0 + cx1) / 2.0, (cy0 + cy1) / 2.0
            pts.update({"n": (mx, cy0), "s": (mx, cy1),
                        "w": (cx0, my), "e": (cx1, my)})
        return pts

    def _hit(self, x, y):
        ix, iy, iw, ih = self._image_rect()
        if iw <= 0:
            return False
        cx0, cy0, cx1, cy1 = self._crop_px()
        for hx, hy in self._handle_points(cx0, cy0, cx1, cy1).values():
            if abs(x - hx) <= self._HANDLE and abs(y - hy) <= self._HANDLE:
                return True
        return cx0 <= x <= cx1 and cy0 <= y <= cy1

    def _on_motion(self, _controller, x, y):
        if not self._active:
            return
        self.set_cursor(self._pointer if self._hit(x, y) else None)

    def _on_drag_begin(self, _gesture, start_x, start_y):
        self._drag_handle = None
        self._drag_start = list(self._crop)
        ix, iy, iw, ih = self._image_rect()
        if iw <= 0:
            return
        cx0, cy0, cx1, cy1 = self._crop_px()
        for name, (hx, hy) in self._handle_points(cx0, cy0, cx1, cy1).items():
            if abs(start_x - hx) <= self._HANDLE and abs(start_y - hy) <= self._HANDLE:
                self._drag_handle = name
                return
        if cx0 <= start_x <= cx1 and cy0 <= start_y <= cy1:
            self._drag_handle = "move"

    def _on_drag_update(self, _gesture, off_x, off_y):
        if self._drag_handle is None:
            return
        ix, iy, iw, ih = self._image_rect()
        if iw <= 0 or ih <= 0:
            return
        dx, dy = off_x / iw, off_y / ih
        x0, y0, x1, y1 = self._drag_start
        m = self._MIN
        h = self._drag_handle
        if h == "move":
            w, ht = x1 - x0, y1 - y0
            nx0 = min(max(0.0, x0 + dx), 1.0 - w)
            ny0 = min(max(0.0, y0 + dy), 1.0 - ht)
            self._crop = [nx0, ny0, nx0 + w, ny0 + ht]
        elif self._aspect is not None:
            self._crop = self._resize_locked(h, dx, dy)
        else:
            if "w" in h:
                x0 = min(max(0.0, x0 + dx), x1 - m)
            if "e" in h:
                x1 = max(min(1.0, x1 + dx), x0 + m)
            if "n" in h:
                y0 = min(max(0.0, y0 + dy), y1 - m)
            if "s" in h:
                y1 = max(min(1.0, y1 + dy), y0 + m)
            self._crop = [x0, y0, x1, y1]
        self.queue_draw()

    def _resize_locked(self, handle, dx, dy):
        """Resize a corner while keeping the locked aspect ratio, anchored at the
        opposite corner. `dx, dy` are normalised drag offsets from drag start."""
        r = self._aspect  # normalised width / height
        x0, y0, x1, y1 = self._drag_start
        # (anchor_x, anchor_y, x_grows_positive, y_grows_positive)
        anchors = {"se": (x0, y0, 1, 1), "sw": (x1, y0, -1, 1),
                   "ne": (x0, y1, 1, -1), "nw": (x1, y1, -1, -1)}
        ax, ay, sgx, sgy = anchors[handle]
        # Tentative moving corner, then size to the larger of the two deltas.
        tx = (x1 if sgx > 0 else x0) + dx
        ty = (y1 if sgy > 0 else y0) + dy
        w = max(abs(tx - ax), abs(ty - ay) * r, self._MIN)
        # Clamp so the box stays inside [0,1] on both axes.
        max_w_x = (1.0 - ax) if sgx > 0 else ax
        max_w_y = ((1.0 - ay) if sgy > 0 else ay) * r
        w = min(w, max_w_x, max_w_y)
        ht = w / r
        nx0, nx1 = (ax, ax + w) if sgx > 0 else (ax - w, ax)
        ny0, ny1 = (ay, ay + ht) if sgy > 0 else (ay - ht, ay)
        return [nx0, ny0, nx1, ny1]

    def _on_drag_end(self, _gesture, _off_x, _off_y):
        self._drag_handle = None
        self._drag_start = None


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


POINTER_CURSOR = Gdk.Cursor.new_from_name("pointer")


def _rgba(r, g, b, a):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


# Pin accent — Easel Blue (the interactive token). A single shade reads well on
# both the light and dark map backdrop, matching how the CSS uses it.
_PIN = _rgba(0x55 / 255, 0x65 / 255, 0xBF / 255, 1.0)
_WHITE = _rgba(1.0, 1.0, 1.0, 1.0)


class MapView(Gtk.Widget):
    """An offline, zoomable vector world map that pins photos by location.

    Easel is offline-first, so the map is drawn from a tiny built-in set of
    coastline polygons (worldmap.py) — no tiles, no network, no dependency, and
    crisp at any zoom because it's vector. Scroll or pinch to zoom, drag to pan.
    Photos are projected equirectangularly and clustered by screen distance, so
    nearby shots share one pin and separate as you zoom in; clicking a pin opens
    all of its photos.

    Everything is painted with GSK nodes in do_snapshot — filled/stroked paths
    for land, rounded-clip circles and a text layout for pins — the drawing path
    that paints reliably in this runtime, so there are no render-to-texture
    surprises."""

    __gtype_name__ = "EaselMapView"

    _CLUSTER_CELL = 44.0   # px grid that merges nearby pins into one
    _MIN_ZOOM = 1.0
    _MAX_ZOOM = 48.0

    def __init__(self):
        super().__init__()
        self._entries = []          # [(lon, lat, photo), …]
        self._clusters = []         # cached clusters for the current view
        self._cache_key = None
        self._hover = -1
        self._pointer = None
        self._activate_cb = None
        self._zoom = 1.0
        self._cx = 0.5              # normalised viewport centre across longitude
        self._cy = 0.5              # normalised viewport centre across latitude
        self._drag_origin = None
        self._zoom_start = 1.0
        self.set_hexpand(True)
        self.set_vexpand(True)

        click = Gtk.GestureClick(button=1)
        click.connect("released", self._on_click)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)
        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        self.add_controller(drag)
        zoom = Gtk.GestureZoom()
        zoom.connect("begin", self._on_zoom_begin)
        zoom.connect("scale-changed", self._on_zoom_scale)
        self.add_controller(zoom)

    def do_measure(self, orientation, for_size):
        return (0, 0, -1, -1)

    def set_photos(self, entries):
        """entries: an iterable of (lon, lat, photo) — photo is opaque to the
        map and handed back to the activate callback on click."""
        self._entries = list(entries)
        self._cache_key = None
        self.queue_draw()

    def set_activate_cb(self, cb):
        """cb(list_of_photos) is called when a pin is clicked."""
        self._activate_cb = cb

    # ---- projection ----

    def _base_w(self):
        """World width in px at zoom 1: the whole world, contained 2:1."""
        w, h = self.get_width(), self.get_height()
        return max(1.0, min(float(w), 2.0 * float(h)))

    def _world_size(self):
        ww = self._base_w() * self._zoom
        return ww, ww / 2.0

    def _project(self, lon, lat):
        w, h = self.get_width(), self.get_height()
        ww, wh = self._world_size()
        u = (lon + 180.0) / 360.0
        v = (90.0 - lat) / 180.0
        return w / 2.0 + (u - self._cx) * ww, h / 2.0 + (v - self._cy) * wh

    # ---- zoom / pan ----

    def _clamp(self):
        self._zoom = max(self._MIN_ZOOM, min(self._MAX_ZOOM, self._zoom))
        self._cx = min(1.0, max(0.0, self._cx))
        self._cy = min(1.0, max(0.0, self._cy))

    def _zoom_at(self, factor, px, py):
        """Zoom by `factor`, keeping the world point under (px, py) fixed."""
        w, h = self.get_width(), self.get_height()
        ww, wh = self._world_size()
        u = self._cx + (px - w / 2.0) / ww
        v = self._cy + (py - h / 2.0) / wh
        self._zoom *= factor
        self._clamp()
        ww, wh = self._world_size()
        self._cx = u - (px - w / 2.0) / ww
        self._cy = v - (py - h / 2.0) / wh
        self._clamp()
        self._cache_key = None
        self.queue_draw()

    def _on_scroll(self, _ctrl, _dx, dy):
        factor = (1.0 / 1.2) if dy > 0 else 1.2   # scroll up zooms in
        px, py = self._pointer or (self.get_width() / 2.0, self.get_height() / 2.0)
        self._zoom_at(factor, px, py)
        return True

    def _on_drag_begin(self, _g, _x, _y):
        self._drag_origin = (self._cx, self._cy)

    def _on_drag_update(self, _g, ox, oy):
        if self._drag_origin is None:
            return
        ww, wh = self._world_size()
        self._cx = self._drag_origin[0] - ox / ww
        self._cy = self._drag_origin[1] - oy / wh
        self._clamp()
        self._cache_key = None
        self.queue_draw()

    def _on_zoom_begin(self, _g, _seq):
        self._zoom_start = self._zoom

    def _on_zoom_scale(self, gesture, scale):
        ok, x, y = gesture.get_bounding_box_center()
        px, py = (x, y) if ok else (self.get_width() / 2.0, self.get_height() / 2.0)
        target = self._zoom_start * scale
        if self._zoom > 0:
            self._zoom_at(target / self._zoom, px, py)

    # ---- clustering ----

    def _ensure_clusters(self):
        """Group photos into pins by screen distance for the current view.
        Depends on zoom and centre, so photos re-cluster as you zoom — nearby
        shots merge when zoomed out and separate as you zoom in."""
        key = (self.get_width(), self.get_height(), round(self._zoom, 4),
               round(self._cx, 5), round(self._cy, 5),
               id(self._entries), len(self._entries))
        if key == self._cache_key:
            return
        self._cache_key = key
        clusters = []
        w, h = self.get_width(), self.get_height()
        if w > 0 and h > 0 and self._entries:
            cell = self._CLUSTER_CELL
            buckets = {}
            for lon, lat, photo in self._entries:
                px, py = self._project(lon, lat)
                bkey = (int(px // cell), int(py // cell))
                buckets.setdefault(bkey, []).append((px, py, photo))
            for members in buckets.values():
                cx = sum(m[0] for m in members) / len(members)
                cy = sum(m[1] for m in members) / len(members)
                clusters.append({
                    "x": cx, "y": cy, "n": len(members),
                    "photos": [m[2] for m in members],
                })
            clusters.sort(key=lambda c: c["n"])  # bigger pins drawn on top
        self._clusters = clusters

    @staticmethod
    def _pin_radius(n):
        return 8.0 + min(9.0, 2.4 * math.log2(n + 1))

    # ---- interaction ----

    def _hit(self, x, y):
        self._ensure_clusters()
        best = -1
        for i, c in enumerate(self._clusters):
            r = self._pin_radius(c["n"]) + 4.0
            if (x - c["x"]) ** 2 + (y - c["y"]) ** 2 <= r * r:
                best = i  # later (larger) pins win ties — they're drawn on top
        return best

    def _on_click(self, _gesture, _n, x, y):
        i = self._hit(x, y)
        if i >= 0 and self._activate_cb is not None:
            self._activate_cb(self._clusters[i]["photos"])

    def _on_motion(self, _ctrl, x, y):
        self._pointer = (x, y)
        self._set_hover(self._hit(x, y))

    def _on_leave(self, *_a):
        self._pointer = None
        self._set_hover(-1)

    def _set_hover(self, i):
        if i == self._hover:
            return
        self._hover = i
        self.set_cursor(POINTER_CURSOR if i >= 0 else None)
        self.queue_draw()

    # ---- painting ----

    @staticmethod
    def _circle(snapshot, cx, cy, r, rgba):
        rect = Graphene.Rect().init(cx - r, cy - r, 2 * r, 2 * r)
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(rect, r)
        snapshot.push_rounded_clip(rounded)
        snapshot.append_color(rgba, rect)
        snapshot.pop()

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return

        fg = self.get_color()  # foreground colour → tracks light/dark theme
        sea = _rgba(fg.red, fg.green, fg.blue, 0.04)
        land = _rgba(fg.red, fg.green, fg.blue, 0.14)
        coast = _rgba(fg.red, fg.green, fg.blue, 0.32)

        bounds = Graphene.Rect().init(0, 0, w, h)
        snapshot.push_clip(bounds)
        snapshot.append_color(sea, bounds)

        # Land: filled vector coastline polygons (crisp at any zoom). Even-odd
        # so island/lake rings punch holes correctly.
        builder = Gsk.PathBuilder()
        for ring in worldmap.land_rings():
            first = True
            for lon, lat in ring:
                x, y = self._project(lon, lat)
                if first:
                    builder.move_to(x, y)
                    first = False
                else:
                    builder.line_to(x, y)
            builder.close()
        path = builder.to_path()
        snapshot.append_fill(path, Gsk.FillRule.EVEN_ODD, land)
        snapshot.append_stroke(path, Gsk.Stroke.new(1.0), coast)
        snapshot.pop()  # bounds clip

        # Pins.
        self._ensure_clusters()
        for i, c in enumerate(self._clusters):
            cx, cy = c["x"], c["y"]
            if cx < -40 or cx > w + 40 or cy < -40 or cy > h + 40:
                continue  # skip pins panned off-screen
            r = self._pin_radius(c["n"])
            if i == self._hover:
                r += 2.0
            self._circle(snapshot, cx, cy, r + 2.0, _WHITE)  # ring
            self._circle(snapshot, cx, cy, r, _PIN)          # disc
            if c["n"] > 1:
                self._draw_count(snapshot, cx, cy, r, c["n"])
            else:
                self._circle(snapshot, cx, cy, r * 0.34, _WHITE)  # centre dot

    def _draw_count(self, snapshot, cx, cy, r, n):
        label = str(n) if n < 1000 else "999+"
        layout = self.create_pango_layout(label)
        try:
            ink, logical = layout.get_pixel_extents()
            tw, th = logical.width, logical.height
        except Exception:
            tw, th = r, r
        snapshot.save()
        snapshot.translate(Graphene.Point().init(cx - tw / 2.0, cy - th / 2.0))
        snapshot.append_layout(layout, _WHITE)
        snapshot.restore()


class FacePinLayer(Gtk.Widget):
    """A transparent overlay drawn on top of the info-panel photo preview. It
    shows a small pin for each person tagged in the photo, and turns a click on
    the photo into a normalised (x, y) so the user can drop a new tag exactly
    where that person is ("pin and type a name").

    Like CropOverlay it's an overlay child that paints its own content in
    do_snapshot — the drawing path that reliably paints in this runtime."""

    __gtype_name__ = "EaselFacePinLayer"

    _DOT_R = 6.0

    def __init__(self):
        super().__init__()
        self._faces = []       # [(name, x, y), …] normalised pin positions
        self._place_cb = None
        self.set_cursor(POINTER_CURSOR)
        click = Gtk.GestureClick(button=1)
        click.connect("released", self._on_click)
        self.add_controller(click)
        # Hovering a pin names the person it marks.
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._on_query_tooltip)

    def do_measure(self, orientation, for_size):
        return (0, 0, -1, -1)

    def set_faces(self, faces):
        """faces: iterable of (name, x, y) — name labels the pin on hover."""
        self._faces = [(str(name), float(x), float(y)) for name, x, y in faces]
        self.queue_draw()

    def set_place_cb(self, cb):
        """cb(nx, ny, px, py) fires when the photo is clicked: nx/ny are
        normalised, px/py are widget pixels (to anchor a popover)."""
        self._place_cb = cb

    def _on_click(self, _gesture, _n, x, y):
        w, h = self.get_width(), self.get_height()
        if w > 0 and h > 0 and self._place_cb is not None:
            self._place_cb(x / w, y / h, x, y)

    def _on_query_tooltip(self, _widget, x, y, _keyboard, tooltip):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return False
        hit = self._DOT_R + 3.0
        for name, nx, ny in self._faces:
            cx, cy = nx * w, ny * h
            if (x - cx) ** 2 + (y - cy) ** 2 <= hit * hit:
                tooltip.set_text(name)
                return True
        return False

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        r = self._DOT_R
        for _name, nx, ny in self._faces:
            cx, cy = nx * w, ny * h
            MapView._circle(snapshot, cx, cy, r + 2.0, _WHITE)
            MapView._circle(snapshot, cx, cy, r, _PIN)
