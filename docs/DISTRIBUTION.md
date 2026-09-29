# Distributing Easel

Easel isn't on Flathub (their guidelines exclude AI-assisted projects), so it
ships through GitHub instead. Two paths are set up, and they coexist. Path 2
is the one to point people at: it has the landing page and delivers updates.

## 1. Single-file bundle on Releases

The simplest path: every tagged release carries a `.flatpak` bundle users can
download and double-click.

**How it works** — `.github/workflows/bundle.yml` runs when you push a tag
starting with `v` (e.g. `v0.1.0`). It builds the app in the GNOME 49 Flatpak
runtime and attaches `io.github.drvonmiau.Easel.flatpak` to that tag's release.

**To cut a release**

1. Bump the version in `meson.build` and add a `<release>` entry to
   `data/io.github.drvonmiau.Easel.metainfo.xml.in`.
2. Create the release + tag on GitHub (Releases → Draft a new release → choose
   or create tag `vX.Y.Z` → Publish). Publishing the tag triggers the workflow;
   a couple of minutes later the bundle appears as a release asset.
   (You can also push the tag from the CLI and the workflow still runs — it just
   needs a matching release to attach to, which `softprops/action-gh-release`
   creates if missing.)

**What users do**

```sh
flatpak install --user io.github.drvonmiau.Easel.flatpak
flatpak run io.github.drvonmiau.Easel
```

Trade-off: no automatic updates — users re-download to upgrade. That's what
path 2 solves.

## 2. Signed Flatpak repository and landing page on GitHub Pages

A signed Flatpak repository served from GitHub Pages at <https://drvonmiau.github.io/easel/>, with a
landing page whose Install button opens GNOME Software. Users add the
repository once and then get updates through GNOME Software or
`flatpak update` like any other app — the same setup as Dice.

**How it works** — `.github/workflows/flatpak-repo.yml` runs when a release is
published (and by hand from Actions → "Flatpak repository (GitHub Pages)" →
Run workflow on `main`). It builds the app with
[Flatter](https://github.com/andyholmes/flatter) in the GNOME 49 container,
signs the repository, writes a one-click `io.github.drvonmiau.Easel.flatpakref`,
and deploys the repository together with `pages/` to Pages. Only released
versions reach users, not every commit to `main`.

**One-time setup** (in the GitHub UI)

1. **Settings → Pages → Source: "GitHub Actions".**
2. **Settings → Secrets and variables → Actions → New repository secret**,
   reusing the signing key already made for Dice:
   - `FLATPAK_GPG_KEY` — the full armored private key
     (`gpg --armor --export-secret-keys <KEYID>`), pasted as-is including the
     `-----BEGIN/END PGP PRIVATE KEY BLOCK-----` lines.
   - `FLATPAK_GPG_PASSPHRASE` — its passphrase.

   Never commit the key or passphrase, or paste them anywhere else.
3. Publish a release (or run the workflow by hand on `main`).

**If a run fails**

- "GPG Agent: No pinentry" — `FLATPAK_GPG_PASSPHRASE` is missing or wrong.
- "Misformed armored text" — `FLATPAK_GPG_KEY` wasn't pasted as the complete
  armored block.
- A run from any branch other than `main` fails at "Deploy to Pages": the
  `github-pages` environment only deploys from `main`. That's expected.

**The landing page** is `pages/index.html`, one self-contained file.
Screenshots live in `pages/media/` as WebP. Flatter copies every file listed
under `upload-pages-includes` to the **site root by file name**, so reference
them in the HTML by bare name, keep names unique, and add any new media file
to that list.

**What users do** — click Install on <https://drvonmiau.github.io/easel/>, or from a terminal:

```sh
flatpak remote-add --user --if-not-exists easel https://drvonmiau.github.io/easel/index.flatpakrepo
flatpak install --user easel io.github.drvonmiau.Easel
```

To remove it again:

```sh
flatpak uninstall --user io.github.drvonmiau.Easel
flatpak remote-delete --user easel
```
