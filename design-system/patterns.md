# Patterns & Components

The reusable pieces of the Miau design language, as built in Easel. Each entry:
what it is, the tokens it uses, its states, and where it lives in code. Reuse by
name — "use the standard info panel" should be enough to reproduce it.

---

## Color architecture (read first)

The single most important pattern. Neutrals come from **libadwaita semantic
colors**; we define only accents.

```css
/* DO — theme-aware */
.paper       { background-color: @card_bg_color; }
.tab:checked { color: @easel_blue; }

/* DON'T — hardcoded, breaks in light mode */
.paper       { background-color: #313131; }
```

Vocabulary to reach for: `@window_bg_color` (the window), `@view_bg_color` (deep
content wells like the lightbox), `@card_bg_color` (the paper), `@window_fg_color`
(text, use with `alpha()` for dimmed variants). Add exactly one accent
(`@easel_blue`) plus a favourite color. That's it.

Dark/light: libadwaita flips the semantics for you. Only override accents per
theme if the accent needs a lighter variant on dark (`@easel_blue_light`).

---

## Layout: Window → Paper → Content

The skeleton every screen shares.

- **Window** — `@window_bg_color`. Holds a vertical stack: header · tabs · content.
- **Paper** — a `@card_bg_color` surface with `radius.xxl` top corners and the
  `paper` shadow. Content (the photo grid) lives on it. Side margins are **5% of
  window width, fixed** — do not re-center the paper when a side panel opens, or
  the grid jumps sideways (learned the hard way; see gotchas → layout).
- **Info panel** — `layout.info_panel_width` (300px), slides in from the right via
  `GtkRevealer` (`slide-left`, 300ms). The paper reflows into the remaining width;
  margins stay put.

Code: `EaselWindow._apply_layout_metrics`, `window.ui` content stack.

---

## Window chrome

Custom titlebar, quiet and monospaced.

- **Window controls** — standard GNOME close/min/max, left.
- **Thumbnail-size slider** — a `GtkScale` living where a media app would put a
  volume slider. Track `alpha(@window_fg_color, 0.18)`, fill + knob `@easel_blue`,
  knob `radius.full`.
- **Hamburger menu** — primary menu, right.

Code: `style.css` → "custom titlebar", "thumbnail-size slider".

---

## Tabs bar

Primary navigation as a row of monospace **pill tabs**.

- Container: `radius.xl`, subtle `alpha(@window_fg_color, 0.06)` background.
- Tab pill: `radius.md`, `type.body` monospace. Inactive = `alpha(@window_fg_color,
  0.55)`. **Active** = filled pill, near-full-opacity fg, `medium` weight.
- Trailing **search** toggle icon at the far right.

Easel tabs: All Photos · Months · Years · Albums · Favourites · Maps · People.
Bind `<primary>1..N` accelerators to tab switching.

Code: `window.ui` titlebar, `_select_tab`.

---

## Photo tile (Swatch)

The core grid cell. A square, self-drawn artwork swatch.

- **Cover-crop**: scale to fill, center-crop, clip to `radius.lg`. Draw the texture
  directly in `do_snapshot` (`append_texture`), **not** via a child `GtkPicture`.
- **Placeholder**: diagonal-striped fill in the widget's CSS `color` while the
  thumbnail decodes (or if it can't be decoded).
- **Overlays** (on hover / when set): favourite **heart** bottom-right (fills in
  place when toggled — gold when faved), context-menu button top-right, video/play
  badge center. Overlay backdrops use `overlay.badge_dim`.
- **Selected**: `elevation.focus_ring` (`box-shadow: 0 0 0 3px @easel_blue`). Click
  again to deselect.

Grid: `GtkGridView`, `layout.grid_gap`, min/max columns 2/12. Keep the detail grid
identical to the main grid — no stray `vexpand` on the GridView or rows spread out.

Code: `widgets.Swatch`, `_make_tile`, `_bind_tile`.

---

## Info panel

Fixed 300px right panel for a selected photo. Single-click opens it; click the tile
again (or close) to dismiss.

Top-to-bottom:
1. **Photo** — square, top-aligned with the paper, `radius.xl`, cover-cropped, with
   the favourite heart bottom-right (padding = `space.xl`).
2. **Action row** — flat icons on the panel background: rotate-left, rotate-right,
   **fullscreen** (primary, filled `@easel_blue`, `radius.xl`, flexes wide), add,
   edit. Grey buttons: `alpha(@window_fg_color, 0.08)`, `radius.sm`.
3. **File details** — monospace rows: filename (`medium`), then Date · Dimensions ·
   Size · Album as label/value pairs (label `text_soft`, value `alpha(fg, 0.84)`).

Non-actions (rotate/fullscreen) are non-destructive — rotation is stored in the DB
and applied at render time, never written back to the file.

Code: `_setup_info_panel`, `_show_info`, `window.ui` info panel; Figma frame
`Easel — Photo selected`.

---

## Buttons

- **Primary** — filled `@easel_blue`, white fg, `radius.xl` (11–12), `shade(@easel_blue, 1.08)` on hover.
- **Flat/icon** — transparent, `alpha(@window_fg_color, 0.08)` background, `radius.sm`,
  `type` icon at ~20px. Used for the action row.
- **Circular** — `radius.full`, for on-photo overlays (fav, menu, lightbox nav).

---

## Non-destructive editor (Easel-specific, reusable idea)

Photo centered on the paper, tools on the right, sliders for
brightness/contrast/saturation applied live via a **GSK color matrix**
(`push_color_matrix`) — original pixels untouched until an explicit "save a copy".
The live preview and the saved output share one render path so they match.

Code: `widgets.AdjustableImage`, `_snapshot_adjusted`, `render_adjusted_texture`.
