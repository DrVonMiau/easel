<p align="center">
  <img src="data/icons/hicolor/512x512/apps/io.github.drvonmiau.Easel.png" width="128" alt="Easel icon">
</p>

<h1 align="center">Easel</h1>

<p align="center">
  A calm, focused gallery for the photos you already own —<br>
  no accounts, no cloud, no noise. Just your pictures, beautifully laid out.
</p>

## What it does

Easel scans your photo folders into a local library and lays them out on a
clean paper card — the sibling to [Lyre](https://github.com/DrVonMiau/lyre),
the same design language recast for photos instead of music.

- **Every photo at once**, or grouped into **albums** by folder
- **A full-window lightbox** — click any photo to view it large, with
  left/right navigation and keyboard control
- **Favourites**, kept apart in their own view
- **It remembers**: window size, last tab — quit and pick up where you left off
- Folder watching, and light and dark themes that follow your system

## Install

Grab the latest `.flatpak` bundle from the
[**Releases**](https://github.com/DrVonMiau/easel/releases) page, then
install and run it:

```sh
flatpak install --user io.github.drvonmiau.Easel.flatpak
flatpak run io.github.drvonmiau.Easel
```

The first command may offer to pull in the GNOME runtime the app needs —
say yes. You only need [Flatpak](https://flatpak.org/setup/) installed,
which most Linux distributions already have.

## Building from source

Open the project in **GNOME Builder** and press Run — the included Flatpak
manifest (`io.github.drvonmiau.Easel.json`) takes care of everything,
including the IBM Plex fonts the design uses.

Or with flatpak-builder directly:

```sh
flatpak-builder --user --install --force-clean _flatpak io.github.drvonmiau.Easel.json
flatpak run io.github.drvonmiau.Easel
```

## License

Easel is free software, released under the
[GNU GPL 3.0 or later](COPYING).
