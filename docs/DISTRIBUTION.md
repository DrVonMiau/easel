# Distributing Easel

Easel isn't on Flathub (their guidelines exclude AI-assisted projects), so it
ships through GitHub instead. Two paths are set up, and they can coexist.

## 1. Single-file bundle on Releases (active now)

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

## 2. Hosted Flatpak repo on GitHub Pages (scaffolded, activate later)

A static Flatpak repository served from GitHub Pages. Users add it once as a
remote and then get updates through GNOME Software / `flatpak update` like any
store app.

**How it works** — `.github/workflows/flatpak-repo.yml` builds the repo with
[Flatter](https://github.com/andyholmes/flatter) and deploys it to Pages. It's
`workflow_dispatch`-only (manual) until you enable it, so it won't fail on every
push before Pages exists.

**To activate**

1. **Settings → Pages → Source: "GitHub Actions".**
2. *(Recommended)* Sign the repo so users don't need `--no-gpg-verify`:
   ```sh
   gpg --quick-gen-key "Easel <you@example.com>"
   gpg --armor --export <KEYID> > easel.gpg                # public — ship this
   gpg --armor --export-secret-keys <KEYID>                # private — copy output
   ```
   Add two repo secrets — `FLATPAK_GPG_KEY` (the private-key block) and
   `FLATPAK_GPG_KEYID` (the key id) — then uncomment the two signing lines in the
   workflow.
3. In the workflow, uncomment the `push: branches: [main]` trigger so every push
   republishes the repo.

**What users do** (once your Pages URL is live, e.g. `https://drvonmiau.github.io/easel`)

```sh
flatpak remote-add --user easel https://drvonmiau.github.io/easel/index.flatpakrepo
flatpak install --user easel io.github.drvonmiau.Easel
```

From then on `flatpak update` pulls new versions automatically. This is the
backend for the download page you mentioned wanting to build — the page just
links to the `.flatpakrepo` file and shows the two commands above.
