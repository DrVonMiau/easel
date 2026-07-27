# Easel — design system

A small, living design system for Easel: the tokens and components that keep
design and development consistent and fast. It shares a design *language* with
its sibling app [Lyre](https://github.com/DrVonMiau/lyre) — the same system
recast for photos instead of music.

- **Source of truth:** the Easel design file in Figma —
  <https://www.figma.com/design/Yvbbs6iPnik7hjXjTwc65v/Dr.-von-Miau>.
  Colours and spacing are defined there first; this document and
  `src/style.css` follow it.
- **Where it lives in code:** colour tokens are `@define-color` entries at the
  top of [`src/style.css`](../src/style.css); the spacing, radius, type and
  elevation scales are documented here and applied by hand (GTK CSS can only
  name colours, not lengths).

> **Note on Figma vs. code.** The Figma file is authored in **dark mode** and
> renders labels in Roboto Mono as a stand-in for the bundled **IBM Plex
> Mono**. Read grey values there as the *dark-theme* realisation of the neutral
> scale, and read the mono type as IBM Plex Mono.

---

## Foundations

### Colour

Neutral surfaces and primary text come from **libadwaita's named system
colours**, so both light and dark track the OS Adwaita palette automatically.
Figma's greys are the dark-theme values of that same neutral scale — we let
libadwaita supply them rather than hard-coding, which is also why light mode
needs no separate Figma spec: it is *just styling* derived from the system.

Colour beyond the neutrals is reserved for **accents**.

| Token | Figma | Light | Dark | Used for |
|---|---|---|---|---|
| `@easel_blue` | Blue 200 | `#5565bf` | — | Interactive: buttons, sliders, links, focus ring, selection |
| `@easel_blue_light` | Blue 100 | — | `#7a89dd` | The same accents on dark surfaces |
| `@easel_gold` | *(Easel extension)* | `#c1962b` | — | Favourites (heart) |
| `@easel_gold_light` | *(Easel extension)* | — | `#ddb964` | Favourites on dark surfaces |
| `@easel_text_soft` | *(Easel extension)* | `#5a6a9e` | — | Secondary / mono-dim text |
| `@easel_text_soft_dark` | *(Easel extension)* | — | `#a7b2da` | Secondary text on dark surfaces |

Neutral scale (do **not** hard-code — use the libadwaita name):

| Figma | Value (dark) | libadwaita name |
|---|---|---|
| Grey 200 | `#4c4c4f` | `@card_bg_color` (active tab, raised surfaces) |
| Grey 300 | `#2c2c2c` | `@window_bg_color` |
| Grey 400 | `#242424` | `@view_bg_color` |
| White | `#ffffff` | `@window_fg_color` (as text) / literal white on photos & overlays |

### Spacing

Figma scale, in px. Snap new margins, padding and gaps to a step.

| Token | Figma | px |
|---|---|---|
| XS | Spase XS | 4 |
| S | Space S | 8 |
| M | Space M | 16 |
| L | Space L | 24 |

Applied: paper content inset = **L (24)**, photo-grid gap = **M (16)**,
titlebar→tabs gap and info-panel rhythm = **M (16)**.

### Radius

| Role | px | Figma | Applies to |
|---|---|---|---|
| Control / tab | 8 | Corners M | `.tab-btn`, `.search-entry`, `.info-action` |
| Button | 10 | *(from `.btn`)* | `.back-btn`, `.empty-cta`, `.info-fs-btn` |
| Card | 14 | — | `.paper` (top corners) |
| Pill / full | 9999 | — | circular icon buttons, badges, slider handles |

### Type

Two families, both bundled by the Flatpak manifest:

- **IBM Plex Sans** — UI and body text.
- **IBM Plex Mono** — labels, eyebrows, tabs, metadata keys (the "technical"
  voice of the design).

Weights: 400 (regular) · 500 (medium) · 600 (semibold).

| Role | Family | Size | Weight | Tracking | Class |
|---|---|---|---|---|---|
| Display heading | Sans | 30 | 600 | -0.01em | `.artist-heading` |
| Section title | Sans | 16 | 600 | — | `.group-header`, `.editor-title` |
| Card / info title | Sans | 15–13.5 | 600 | — | `.info-title`, `.card-title` |
| Body / value | Sans | 13 | 400 | — | `.info-value`, `.editor-slider-label` |
| Tab / button | Mono | 13 | 500 | 0.04–0.06em | `.tab-btn`, `.back-btn` |
| Metadata / caption | Mono | 12 | 400 | — | `.mono-dim`, `.search-entry` |
| Eyebrow | Mono | 11 | 400 | 0.14em | `.eyebrow`, `.editor-eyebrow` |
| Key (uppercase-ish) | Mono | 10 | 400 | 0.12em | `.info-key` |

### Elevation

| Role | Light | Dark |
|---|---|---|
| Paper card | `0 1px 2px alpha(#000,.04), 0 12px 32px alpha(#000,.08)` | `…,.2 / …,.35` |
| Figma "Shadow" | `0 6px 24px alpha(#000,.35)` | same |

Thumbnails are intentionally **flat** (no shadow) so they sit cleanly on the
paper.

---

## Components

Each Figma component maps to one or more CSS classes on a GTK widget. Reuse
these before inventing new styling.

| Figma component | CSS class(es) | GTK widget | Notes |
|---|---|---|---|
| `.btn` · Primary | `.empty-cta`, `.info-fs-btn`, `.editor-save` | `GtkButton` | Blue fill, white label, radius 10 |
| `.btn` · Secondary | *(system default)* | `GtkButton` | Neutral fill (Grey 200 in dark) |
| `.btn` · Tertiary | `.back-btn` | `GtkButton` | Bordered, transparent fill, radius 10 |
| `..tab` · Active | `.tab-btn.tab-active` | `GtkButton` | Raised pill; Grey 200 in dark, card + shadow in light |
| `..tab` · Inactive | `.tab-btn` | `GtkButton` | Dim label, no fill |
| `.tabs` (group) | `.tab-group` | `GtkBox` | Soft rounded container holding the tabs |
| `.photo_tumbnail` | `.swatch`, `.card-swatch` | `EaselSwatch` | Square, radius 10, cover-cropped; striped placeholder when empty |
| — heart overlay | `.tile-fav` / `.tile-fav.faved` | `GtkButton` | White on dark badge → gold when favourited |
| — menu overlay | `.card-menu-btn` | `GtkButton` | Circular, dark scrim |
| — video badge | `.video-badge` | `GtkImage` | Centred play glyph |
| Search | `.search-entry` | `GtkSearchEntry` | Mono, radius 8, blue focus border |
| Thumb-size slider | `.thumb-scale` | `GtkScale` | Thin track, blue handle on hover |
| Info panel | `.info-panel`, `.info-preview`, `.info-key`, `.info-value` | `GtkBox` | Preview + quick actions + metadata rows |
| Quick action | `.info-action` | `GtkButton` | Flat icon, radius 8 |
| Lightbox | `.lightbox`, `.lightbox-nav`, `.lightbox-btn`, `.lightbox-caption` | overlay | Full-window dark viewer |
| Editor | `.editor`, `.edit-tools`, `.editor-scale`, `.editor-tool-btn` | overlay | Dark editing surface |

---

## Theming (light / dark)

- The window carries a `.dark` class mirroring libadwaita's dark state.
- Neutral surfaces and text adapt on their own through the libadwaita named
  colours — no per-theme rules needed for those.
- Only **accents** are swapped for dark: `@easel_blue` → `@easel_blue_light`,
  `@easel_gold` → `@easel_gold_light`, secondary text → its `_dark` tint. See
  the `window.dark …` block at the bottom of `src/style.css`.
- Light mode is not separately specified in Figma — it is the same tokens with
  system-derived neutrals.

---

## Reusing this in a sibling app

There is no standalone design-system repo yet, and for two small GTK/Flatpak
apps that is the right call — the token surface is a single CSS file, and a
shared repo would mean submodules or vendoring for little gain. To adopt this
language in **Lyre**:

1. Copy the token block from the top of `src/style.css` and this guide.
2. Keep the Figma file as the source of truth for colours and spacing.
3. Reuse the component classes above; only add app-specific ones alongside.

Extract to a dedicated repo only once keeping two copies in sync actually
hurts — the guide makes that a lift-and-shift, not a rewrite.
