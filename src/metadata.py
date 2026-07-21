"""Fetch album covers + artist photos from free, keyless services:
MusicBrainz (+ Cover Art Archive) for albums, Wikidata/Wikipedia for artist photos.

Search results are ranked before use: MusicBrainz's first hit is often a
compilation, bootleg or wrong region, so releases are scored on how well
title/artist match, their official status and MusicBrainz's own confidence,
and cover art is tried across the best few candidates (release first, then
its release group) instead of trusting hit #1.
"""
import json
import urllib.parse
import urllib.request

import musicbrainzngs as mb

from . import library as lib

mb.set_useragent("Lyre", "1.0", "https://github.com/DrVonMiau/lyre")
mb.set_rate_limit(True)


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Lyre/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


def _norm(s):
    """Lowercase and strip punctuation so 'Låt It Be!' == 'lat it be'-ish
    comparisons don't fail on cosmetic differences."""
    return "".join(c for c in (s or "").casefold() if c.isalnum() or c.isspace()).strip()


def _score_release(rel, want_artist, want_title):
    s = 0.0
    if _norm(rel.get("title")) == _norm(want_title):
        s += 3.0
    if _norm(rel.get("artist-credit-phrase")) == _norm(want_artist):
        s += 2.0
    # MusicBrainz's own match confidence (0-100).
    s += int(rel.get("ext:score", 0)) / 100.0
    if (rel.get("status") or "").lower() == "official":
        s += 1.0
    # Albums proper over singles/compilations when the info is there.
    group = rel.get("release-group", {})
    if (group.get("primary-type") or "").lower() == "album":
        s += 0.5
    if rel.get("date"):
        s += 0.25
    return s


def _try_cover(rel):
    """Try the release's front cover, then its release group's. Returns
    image bytes or None."""
    try:
        return _get(f"https://coverartarchive.org/release/{rel['id']}/front-500")
    except Exception:
        pass
    group_id = rel.get("release-group", {}).get("id")
    if group_id:
        try:
            return _get(f"https://coverartarchive.org/release-group/{group_id}/front-500")
        except Exception:
            pass
    return None


def fetch_album(con, album_row):
    try:
        res = mb.search_releases(
            artist=album_row["artist_name"], release=album_row["title"], limit=8
        )
    except Exception:
        return
    rels = res.get("release-list", [])
    if not rels:
        con.execute("UPDATE albums SET info_fetched=1 WHERE id=?", (album_row["id"],))
        con.commit()
        return
    rels.sort(
        key=lambda r: _score_release(r, album_row["artist_name"], album_row["title"]),
        reverse=True,
    )

    chosen = rels[0]
    cover_path = None
    # An embedded or hand-picked cover always wins; only fetch when missing.
    if not album_row["cover_path"]:
        for rel in rels[:5]:
            data = _try_cover(rel)
            if data:
                cover = lib.COVERS_DIR / f"{album_row['id']}.jpg"
                cover.write_bytes(data)
                cover_path = str(cover)
                chosen = rel
                break

    date = chosen.get("date")
    year = int(date[:4]) if date and date[:4].isdigit() else None
    con.execute(
        "UPDATE albums SET cover_path=COALESCE(?, cover_path), mb_id=?, info_fetched=1, year=COALESCE(?, year) WHERE id=?",
        (cover_path, chosen["id"], year, album_row["id"]),
    )
    con.commit()


def fetch_artist(con, artist_row):
    name = artist_row["name"]
    try:
        res = mb.search_artists(artist=name, limit=5)
        arts = res.get("artist-list", [])
        if not arts:
            con.execute("UPDATE artists SET info_fetched=1 WHERE id=?", (artist_row["id"],))
            con.commit()
            return
        # Prefer an exact name (or alias-free) match over MusicBrainz's first hit.
        exact = [a for a in arts if _norm(a.get("name")) == _norm(name)]
        aid = (exact[0] if exact else arts[0])["id"]
        full = mb.get_artist_by_id(aid, includes=["url-rels"])["artist"]
        wikidata_url = next(
            (r["target"] for r in full.get("url-relation-list", []) if r.get("type") == "wikidata"),
            None,
        )
        photo_path = None
        if wikidata_url:
            qid = wikidata_url.rstrip("/").split("/")[-1]
            wd = json.loads(_get(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"))
            title = wd["entities"][qid]["sitelinks"].get("enwiki", {}).get("title")
            if title:
                summary = json.loads(
                    _get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}")
                )
                thumb = summary.get("thumbnail", {}).get("source")
                if thumb:
                    photo = lib.PHOTOS_DIR / f"{artist_row['id']}.jpg"
                    photo.write_bytes(_get(thumb))
                    photo_path = str(photo)
        con.execute(
            "UPDATE artists SET photo_path=COALESCE(?, photo_path), mb_id=?, info_fetched=1 WHERE id=?",
            (photo_path, aid, artist_row["id"]),
        )
        con.commit()
    except Exception:
        pass


def fetch_all_missing(con):
    """Fetch info for every artist/album not yet fetched. Call from a worker thread."""
    for a in con.execute("SELECT * FROM artists WHERE info_fetched=0").fetchall():
        fetch_artist(con, a)
    albums = con.execute(
        """SELECT albums.*, artists.name AS artist_name FROM albums
           JOIN artists ON artists.id = albums.artist_id
           WHERE albums.info_fetched=0"""
    ).fetchall()
    for al in albums:
        fetch_album(con, al)
