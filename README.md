<p align="center">
  <img src="data/icons/hicolor/512x512/apps/io.github.drvonmiau.Easel.png" width="120" alt="Easel icon">
</p>

<h1 align="center">Easel</h1>

<p align="center">
  A calm, offline gallery for the photos you already own —<br>
  no accounts, no cloud, no noise. Just your pictures, beautifully laid out.
</p>

<p align="center">
  <img src="data/screenshots/gallery.png" width="820" alt="Easel showing all photos">
</p>

Easel scans your photo folders into a local library and lays them out on a
clean paper card. Browse everything at once or by folder, album, person or
place; open any photo full-window; and make quick edits that never touch the
original file.

<p align="center">
  <img src="data/screenshots/details.png" width="49%" alt="A photo with its details panel">
  <img src="data/screenshots/editor.png" width="49%" alt="The non-destructive editor">
</p>

## Features

**See your photos**
- **Every photo at once**, or grouped into **Folders, Albums, People** and a **Map**
- A **full-window lightbox** with left/right and keyboard navigation
- **Favourites**, kept apart in their own view

**Edit, without touching the original**
- **Non-destructive adjustments**: brightness, contrast, saturation, exposure
  and temperature
- **Rotate, flip and crop**, plus one-tap filters — B&amp;W, Sepia, Warm, Cool,
  Vivid, Fade and Noir
- Saves a **new copy** and **preserves your EXIF** data

**Built for real libraries**
- **HEIC and video** support, with video thumbnails
- Your photos placed on an **offline OpenStreetMap** — no tiles ever fetched
- Sorted by **EXIF capture date**; **folder watching**; **Move to Trash** through
  the desktop portal
- **It remembers** window size and last tab; light and dark themes that follow
  the system

## Install

Get Easel from **<https://drvonmiau.github.io/easel/>** — the Install button opens GNOME Software, and
updates then arrive like any other app's. Or from a terminal:

```sh
flatpak remote-add --user --if-not-exists easel https://drvonmiau.github.io/easel/index.flatpakrepo
flatpak install --user easel io.github.drvonmiau.Easel
```

You only need [Flatpak](https://flatpak.org/setup/), which most Linux
distributions already have. Easel isn't on Flathub, which doesn't accept apps
made with AI assistance; it ships from its own signed repository instead.
Each [release](https://github.com/DrVonMiau/easel/releases) also carries a
single-file `.flatpak` bundle, which doesn't update itself.

## Building from source

Open the project in **GNOME Builder** and press Run — the included Flatpak
manifest (`io.github.drvonmiau.Easel.json`) takes care of everything, including
the IBM Plex fonts the design uses.

Or with `flatpak-builder` directly:

```sh
flatpak-builder --user --install --force-clean _flatpak io.github.drvonmiau.Easel.json
flatpak run io.github.drvonmiau.Easel
```

## Part of a family

Easel is one of three sibling apps that share a design language — the same calm,
offline-first idea recast for different libraries:

- 🖼️ **Easel** — your photos *(you are here)*
- 🎵 [**Lyre**](https://github.com/DrVonMiau/lyre) — your music
- 📖 [**Quill**](https://github.com/DrVonMiau/quill) — your reading

## Built with

GTK4 · libadwaita · PyGObject, packaged as a Flatpak on the GNOME runtime.

## License

Easel is free software, released under the
[GNU GPL 3.0 or later](COPYING).
