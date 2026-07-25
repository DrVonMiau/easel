# GNOME / GTK4 Runtime Playbook

The hard-won lessons from building Easel against the GNOME 49 / GTK 4.20 Flatpak
runtime. Most of these cost real debugging time. **Read before writing image,
threading, or Flatpak code** in a new app.

The through-line: the **development GTK** (e.g. 4.14 on the dev box) and the
**Flatpak runtime GTK** (4.20) differ in rendering and image handling. Something
that works in Builder's dev preview can be blank or broken in the packaged app.
Test in the actual runtime.

---

## 1. Decode images with `Gdk.Texture`, never gdk-pixbuf

**The single biggest time-sink of the project.** In the GNOME 49 runtime,
`GdkPixbuf.new_from_file*` delegates decoding to a **glycin subprocess**
(`flatpak-spawn --sandbox … glycin-image-rs`). That subprocess can exit early:

```
gdk-pixbuf-error-quark: Loader process exited early with status '1'
```

Every decode then returns `None` and thumbnails silently fall back to the
placeholder. The tell was in the run console, not the dev preview.

**Fix:** decode with `Gdk.Texture.new_from_filename(path)` — it uses glycin's
native path and works. Never build textures from a `GdkPixbuf` either
(`Gdk.MemoryTexture` / `Gdk.Texture.new_for_pixbuf` render blank in this runtime).

For scaled/rotated thumbnails: load the source with `new_from_filename`, reshape
with **GSK** (`Gtk.Snapshot.append_scaled_texture` → `Gsk.CairoRenderer.render_texture`),
save the result to a **PNG on disk** (`Gdk.Texture.save_to_png_bytes`), and load
that PNG back with `new_from_filename`. What the UI paints is always a plain
file-backed texture. See `widgets._decode_scaled`, `_render_texture`.

> Corollary: if an image "disappears when rotated" but shows unrotated, you're
> looking at a pixbuf-derived texture failing to paint. Same root cause.

---

## 2. Decode on the main thread, in bounded idle batches

glycin's subprocess model only works from the **main thread** — a worker-pool
decode just yields blanks. So you can't thread image loading. Instead:

- Queue requests on a **LIFO stack** (most-recently-scrolled decodes first).
- Process a **small batch per `GLib.idle_add` cycle** (Easel: 2) so the UI stays
  responsive.
- Skip work the caller no longer wants via a `wants()` predicate (recycled tiles).
- Cache decoded textures in a bounded **LRU** (Easel: 320) keyed by
  `(path, size, rotation, mtime)`, plus the on-disk PNG cache from gotcha #1.

This also prevents the **OOM crash**: decoding many full-res images at once (a
12MP photo is ~48 MB decoded) took down the whole machine. Bounded + scaled fixes
it. For Months/Years, aggregate into period *cards* — never one tile per photo.

Code: `widgets.request_thumbnail`, `_process_load_stack`.

---

## 3. GTK 4.20 API differences from 4.14

- `EventControllerMotion.get_contains_pointer()` → gone; use
  `controller.get_property("contains-pointer")`.
- A `GtkPicture` **child inside a custom widget** may not paint in the runtime;
  self-draw the texture in `do_snapshot` (`append_texture`) instead.
- `Gtk.Scale`/`set_filename` edge cases left null-paintable broken states in the
  lightbox — prefer `set_paintable(load_full_texture(...))`.

When something renders in Builder but not in the packaged app, suspect a
4.14-vs-4.20 rendering difference first.

---

## 4. Flatpak sandbox & files

- Reading the user's photos: `--filesystem=home:ro` (or `xdg-pictures:ro`). Files
  the user *picks* arrive via the **document portal** as `/run/user/1000/doc/…`
  paths — those work with `Gdk.Texture.new_from_filename` and plain `open()`.
- Writable cache: `XDG_CACHE_HOME` is writable in the sandbox — put the thumbnail
  PNG cache there (`$XDG_CACHE_HOME/<app>/thumbs/…`). Write atomically (temp file
  + `os.replace`) so a half-written file is never read.
- Save-a-copy needs write access → use the file-chooser **portal**, don't assume a
  writable path.
- `._` AppleDouble sidecar files and hidden dotfiles fail to decode — skip hidden
  files/dirs during scan and purge existing ones on connect.

---

## 5. Metadata & dates

- Photo capture date: read **EXIF `DateTimeOriginal`**, not filesystem mtime
  (copying files rewrites mtime → wrong dates/order). A self-contained JPEG EXIF
  reader (parse APP1 → TIFF IFD → tag 0x9003) avoids a dependency and the broken
  gdk-pixbuf path. Fall back to file birth time (`st_birthtime`), then mtime.
- Dimensions without a full decode: `GdkPixbuf.Pixbuf.get_file_info(path)` reads
  only the header (no glycin subprocess) — safe, unlike the full loaders.
- On rescan, refresh a photo's auto-derived date but **never clobber a
  hand-edited one** (guard: only update if the stored value still equals the old
  mtime).

Code: `library._date_taken`, `_exif_datetime`.

---

## 6. Rendering & GSK

- Live image adjustments (brightness/contrast/saturation): a **GSK color matrix**
  via `snapshot.push_color_matrix(matrix, offset)`. GSK reads the 16 floats
  **column-major** — assemble row-major and transpose.
- To rasterize a snapshot to a texture (for saving): `Gsk.CairoRenderer` →
  `realize(None)` → `render_texture(node, rect)` → `unrealize()`. GSK rendering
  is **not thread-safe** — do it on the main thread.
- Share one render function between live preview and save so they can't drift.

---

## Checklist for a new media app

- [ ] Neutrals from libadwaita semantic colors; only accents defined by us.
- [ ] Images decoded via `Gdk.Texture.new_from_filename`; no gdk-pixbuf loaders.
- [ ] Thumbnails: on-disk PNG cache + bounded main-thread idle decode + LRU.
- [ ] Aggregate large collections into cards; never one widget per item at full res.
- [ ] EXIF dates with fallbacks; skip hidden/sidecar files.
- [ ] Writable state under `XDG_CACHE_HOME`, written atomically.
- [ ] Tested in the **packaged Flatpak runtime**, not just Builder's preview.
