"""Local music library: SQLite storage + folder scanner."""
import base64
import os
import sqlite3
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.flac import Picture

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "musicplayer"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "musicplayer"
COVERS_DIR = CACHE_DIR / "covers"
PHOTOS_DIR = CACHE_DIR / "artists"
DB_PATH = DATA_DIR / "library.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders(id INTEGER PRIMARY KEY, path TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS artists(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, photo_path TEXT,
  mb_id TEXT, info_fetched INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS albums(
  id INTEGER PRIMARY KEY, title TEXT, artist_id INTEGER, year INTEGER,
  cover_path TEXT, mb_id TEXT, info_fetched INTEGER DEFAULT 0,
  UNIQUE(title, artist_id), FOREIGN KEY(artist_id) REFERENCES artists(id));
CREATE TABLE IF NOT EXISTS tracks(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE, title TEXT, artist_id INTEGER,
  album_id INTEGER, track_no INTEGER, duration REAL, mtime REAL,
  favorite INTEGER DEFAULT 0,
  FOREIGN KEY(artist_id) REFERENCES artists(id),
  FOREIGN KEY(album_id) REFERENCES albums(id));
CREATE TABLE IF NOT EXISTS playlists(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS playlist_tracks(
  id INTEGER PRIMARY KEY, playlist_id INTEGER NOT NULL, track_id INTEGER NOT NULL,
  position INTEGER NOT NULL,
  FOREIGN KEY(playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
  FOREIGN KEY(track_id) REFERENCES tracks(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS plays(
  id INTEGER PRIMARY KEY, track_id INTEGER NOT NULL,
  played_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(track_id) REFERENCES tracks(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_plays_track ON plays(track_id);
"""

AUDIO_EXT = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".wav", ".wma", ".aac"}


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    # Migration for databases created before the favourites feature.
    try:
        con.execute("ALTER TABLE tracks ADD COLUMN favorite INTEGER DEFAULT 0")
        con.commit()
    except sqlite3.OperationalError:
        pass
    return con


def add_folder(con, path):
    con.execute("INSERT OR IGNORE INTO folders(path) VALUES (?)", (path,))
    con.commit()


def all_folders(con):
    return con.execute("SELECT id, path FROM folders ORDER BY path").fetchall()


def remove_folder(con, path):
    """Forget a folder and everything scanned from it. Files stay on disk."""
    con.execute("DELETE FROM folders WHERE path=?", (path,))
    con.execute("DELETE FROM tracks WHERE path LIKE ?", (path.rstrip("/") + "/%",))
    prune_orphans(con)


def wipe_library(con):
    """Erase the whole library: tracks, albums, artists, playlists, play
    history and folder list. Audio files on disk are untouched."""
    for table in ("plays", "playlist_tracks", "playlists", "tracks",
                  "albums", "artists", "folders"):
        con.execute(f"DELETE FROM {table}")
    con.commit()


def get_or_create_artist(con, name):
    row = con.execute("SELECT id FROM artists WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    return con.execute("INSERT INTO artists(name) VALUES (?)", (name,)).lastrowid


def get_or_create_album(con, title, artist_id, year):
    row = con.execute(
        "SELECT id FROM albums WHERE title=? AND artist_id=?", (title, artist_id)
    ).fetchone()
    if row:
        return row["id"]
    return con.execute(
        "INSERT INTO albums(title, artist_id, year) VALUES (?,?,?)", (title, artist_id, year)
    ).lastrowid


def _tag(tags, key, default=""):
    v = tags.get(key)
    if isinstance(v, list):
        return v[0] if v else default
    return v if v is not None else default


def _embedded_cover(raw):
    """Front-cover image bytes embedded in the file's tags, or None.
    Handles FLAC pictures, ID3 APIC, MP4 covr and OGG/Opus base64 pictures."""
    try:
        pictures = getattr(raw, "pictures", None)  # FLAC
        if pictures:
            front = [p for p in pictures if getattr(p, "type", 0) == 3]
            return (front[0] if front else pictures[0]).data
        tags = raw.tags
        if tags is None:
            return None
        getall = getattr(tags, "getall", None)  # ID3 (mp3)
        if getall:
            apics = getall("APIC")
            if apics:
                front = [p for p in apics if getattr(p, "type", 0) == 3]
                return (front[0] if front else apics[0]).data
        if "covr" in tags:  # MP4 (m4a)
            covr = tags["covr"]
            if covr:
                return bytes(covr[0])
        block = tags.get("metadata_block_picture")  # OGG Vorbis / Opus
        if block:
            return Picture(base64.b64decode(block[0])).data
    except Exception:
        pass
    return None


def _maybe_embedded_cover(con, raw, album_id):
    """If the album has no cover yet, pull one out of the file's own tags.
    Cheap no-op when a cover (embedded, fetched or custom) already exists."""
    row = con.execute("SELECT cover_path FROM albums WHERE id=?", (album_id,)).fetchone()
    if not row or row["cover_path"]:
        return
    data = _embedded_cover(raw)
    if not data:
        return
    dest = COVERS_DIR / f"embedded-{album_id}.jpg"
    try:
        dest.write_bytes(data)
    except OSError:
        return
    con.execute("UPDATE albums SET cover_path=? WHERE id=?", (str(dest), album_id))
    con.commit()


def scan_file(con, path):
    """Read one audio file's tags into the library (insert or update)."""
    try:
        audio = MutagenFile(path, easy=True)
        raw = MutagenFile(path)
    except Exception:
        return
    if audio is None:
        return
    tags = audio.tags or {}
    title = _tag(tags, "title", Path(path).stem)
    artist = _tag(tags, "artist", "Unknown Artist")
    albumartist = _tag(tags, "albumartist", artist)
    album = _tag(tags, "album", "Unknown Album")
    try:
        track_no = int(str(_tag(tags, "tracknumber", "0")).split("/")[0])
    except ValueError:
        track_no = 0
    date = _tag(tags, "date") or _tag(tags, "year")
    year = None
    for tok in str(date).replace("-", " ").split():
        if len(tok) == 4 and tok.isdigit():
            year = int(tok)
            break
    duration = float(raw.info.length) if raw and raw.info else 0.0
    mtime = os.path.getmtime(path)

    existing = con.execute(
        "SELECT id, mtime, album_id FROM tracks WHERE path=?", (path,)
    ).fetchone()
    if existing and existing["mtime"] == mtime:
        # Unchanged file — but older libraries may predate embedded-cover
        # extraction, so still offer its art to a coverless album.
        _maybe_embedded_cover(con, raw, existing["album_id"])
        return

    artist_id = get_or_create_artist(con, artist)
    albumartist_id = get_or_create_artist(con, albumartist)
    album_id = get_or_create_album(con, album, albumartist_id, year)

    if existing:
        con.execute(
            """UPDATE tracks SET title=?, artist_id=?, album_id=?, track_no=?,
               duration=?, mtime=? WHERE id=?""",
            (title, artist_id, album_id, track_no, duration, mtime, existing["id"]),
        )
    else:
        con.execute(
            """INSERT INTO tracks(path, title, artist_id, album_id, track_no, duration, mtime)
               VALUES (?,?,?,?,?,?,?)""",
            (path, title, artist_id, album_id, track_no, duration, mtime),
        )
    con.commit()
    _maybe_embedded_cover(con, raw, album_id)


def write_tags(path, *, title, artist, album, track_no=0):
    """Write basic tags back to the audio file (used by Edit Metadata)."""
    audio = MutagenFile(path, easy=True)
    if audio is None:
        raise ValueError("Unsupported audio file")
    if audio.tags is None:
        audio.add_tags()
    audio["title"] = title
    audio["artist"] = artist
    audio["album"] = album
    if track_no:
        audio["tracknumber"] = str(track_no)
    audio.save()


def retag_album(con, album_id, *, title, year=None):
    """Rename an album (and optionally set its year) across every file in it.
    Returns the titles of tracks whose files couldn't be written."""
    rows = tracks_by_album(con, album_id)
    failed, sample_path = [], None
    for r in rows:
        try:
            audio = MutagenFile(r["path"], easy=True)
            if audio is None:
                raise ValueError("Unsupported audio file")
            if audio.tags is None:
                audio.add_tags()
            audio["album"] = title
            if year:
                audio["date"] = str(year)
            audio.save()
            scan_file(con, r["path"])
            sample_path = sample_path or r["path"]
        except Exception:
            failed.append(r["title"])
    # scan_file only sets the year when it first creates an album row, so
    # pin it explicitly on the (possibly new) album the tracks landed in.
    if year and sample_path:
        moved = con.execute("SELECT album_id FROM tracks WHERE path=?", (sample_path,)).fetchone()
        if moved:
            con.execute("UPDATE albums SET year=? WHERE id=?", (year, moved["album_id"]))
    prune_orphans(con)
    return failed


def rename_artist(con, artist_id, new_name):
    """Rename an artist across every file credited to them (both the artist
    tag and, where it matched the old name, the albumartist tag).
    Returns the titles of tracks whose files couldn't be written."""
    row = get_artist(con, artist_id)
    if not row:
        return []
    old_name = row["name"]
    failed = []
    for r in tracks_by_artist(con, artist_id):
        try:
            audio = MutagenFile(r["path"], easy=True)
            if audio is None:
                raise ValueError("Unsupported audio file")
            if audio.tags is None:
                audio.add_tags()
            audio["artist"] = new_name
            albumartist = audio.get("albumartist")
            if albumartist and albumartist[0] == old_name:
                audio["albumartist"] = new_name
            audio.save()
            scan_file(con, r["path"])
        except Exception:
            failed.append(r["title"])
    prune_orphans(con)
    return failed


def scan_folder(con, folder, progress_cb=None):
    files = [
        os.path.join(r, f)
        for r, _d, fs in os.walk(folder)
        for f in fs
        if Path(f).suffix.lower() in AUDIO_EXT
    ]
    for i, path in enumerate(files):
        scan_file(con, path)
        if progress_cb:
            progress_cb(i + 1, len(files))
    prune(con, folder)


def prune_orphans(con):
    """Delete albums/artists that no longer have any tracks."""
    con.execute("DELETE FROM albums WHERE id NOT IN (SELECT DISTINCT album_id FROM tracks)")
    con.execute(
        "DELETE FROM artists WHERE id NOT IN (SELECT artist_id FROM tracks UNION SELECT artist_id FROM albums)"
    )
    con.commit()


def prune(con, folder):
    for row in con.execute("SELECT id, path FROM tracks WHERE path LIKE ?", (folder + "%",)).fetchall():
        if not os.path.exists(row["path"]):
            con.execute("DELETE FROM tracks WHERE id=?", (row["id"],))
    prune_orphans(con)


def record_play(con, track_id):
    """Log one play. Not surfaced in the UI yet; feeds future smart views
    (Most Played, Recently Played…)."""
    con.execute("INSERT INTO plays(track_id) VALUES (?)", (track_id,))
    con.commit()


def scan_all(con, progress_cb=None):
    for row in con.execute("SELECT path FROM folders"):
        if os.path.isdir(row["path"]):
            scan_folder(con, row["path"], progress_cb)


# ---------- queries (all return rows with consistent artist_name/album_title columns) ----------

def all_artists(con):
    return con.execute(
        """SELECT artists.*,
             (SELECT COUNT(*) FROM albums WHERE albums.artist_id = artists.id) AS album_count,
             (SELECT COUNT(*) FROM tracks WHERE tracks.artist_id = artists.id) AS track_count
           FROM artists ORDER BY name"""
    ).fetchall()


def all_albums(con):
    return con.execute(
        """SELECT albums.*, artists.name AS artist_name FROM albums
           JOIN artists ON artists.id = albums.artist_id
           ORDER BY artists.name, albums.year"""
    ).fetchall()


def all_tracks(con):
    return con.execute(
        """SELECT tracks.*, artists.name AS artist_name, albums.title AS album_title
           FROM tracks JOIN artists ON artists.id = tracks.artist_id
           JOIN albums ON albums.id = tracks.album_id
           ORDER BY tracks.title"""
    ).fetchall()


def albums_by_artist(con, artist_id):
    return con.execute("SELECT * FROM albums WHERE artist_id=? ORDER BY year", (artist_id,)).fetchall()


def tracks_by_album(con, album_id):
    return con.execute(
        """SELECT tracks.*, artists.name AS artist_name, albums.title AS album_title
           FROM tracks JOIN artists ON artists.id = tracks.artist_id
           JOIN albums ON albums.id = tracks.album_id
           WHERE album_id=? ORDER BY track_no""",
        (album_id,),
    ).fetchall()


def tracks_by_artist(con, artist_id):
    return con.execute(
        """SELECT tracks.*, artists.name AS artist_name, albums.title AS album_title
           FROM tracks JOIN artists ON artists.id = tracks.artist_id
           JOIN albums ON albums.id = tracks.album_id
           WHERE tracks.artist_id=? ORDER BY albums.year, track_no""",
        (artist_id,),
    ).fetchall()


def get_track(con, track_id):
    return con.execute(
        """SELECT tracks.*, artists.name AS artist_name, albums.title AS album_title
           FROM tracks JOIN artists ON artists.id = tracks.artist_id
           JOIN albums ON albums.id = tracks.album_id
           WHERE tracks.id=?""",
        (track_id,),
    ).fetchone()


def get_album(con, album_id):
    return con.execute(
        """SELECT albums.*, artists.name AS artist_name FROM albums
           JOIN artists ON artists.id = albums.artist_id WHERE albums.id=?""",
        (album_id,),
    ).fetchone()


def get_artist(con, artist_id):
    return con.execute("SELECT * FROM artists WHERE id=?", (artist_id,)).fetchone()


# ---------- playlists ----------

def all_playlists(con):
    return con.execute(
        """SELECT p.id, p.name,
             (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id = p.id) AS track_count,
             (SELECT al.cover_path FROM playlist_tracks pt
                JOIN tracks t ON t.id = pt.track_id
                JOIN albums al ON al.id = t.album_id
              WHERE pt.playlist_id = p.id AND al.cover_path IS NOT NULL
              ORDER BY pt.position LIMIT 1) AS cover_path
           FROM playlists p ORDER BY p.name"""
    ).fetchall()


def get_playlist(con, playlist_id):
    return con.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()


def create_playlist(con, name):
    playlist_id = con.execute("INSERT INTO playlists(name) VALUES (?)", (name,)).lastrowid
    con.commit()
    return playlist_id


def rename_playlist(con, playlist_id, name):
    con.execute("UPDATE playlists SET name=? WHERE id=?", (name, playlist_id))
    con.commit()


def delete_playlist(con, playlist_id):
    con.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
    con.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
    con.commit()


def add_to_playlist(con, playlist_id, track_ids):
    row = con.execute(
        "SELECT COALESCE(MAX(position), 0) AS p FROM playlist_tracks WHERE playlist_id=?",
        (playlist_id,),
    ).fetchone()
    position = row["p"]
    for track_id in track_ids:
        position += 1
        con.execute(
            "INSERT INTO playlist_tracks(playlist_id, track_id, position) VALUES (?,?,?)",
            (playlist_id, track_id, position),
        )
    con.commit()


def remove_from_playlist(con, playlist_id, track_id):
    con.execute(
        "DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?",
        (playlist_id, track_id),
    )
    con.commit()


def playlist_tracks(con, playlist_id):
    return con.execute(
        """SELECT tracks.*, artists.name AS artist_name, albums.title AS album_title,
                  albums.cover_path AS cover_path, pt.id AS entry_id
           FROM playlist_tracks pt
           JOIN tracks ON tracks.id = pt.track_id
           JOIN artists ON artists.id = tracks.artist_id
           JOIN albums ON albums.id = tracks.album_id
           WHERE pt.playlist_id=? ORDER BY pt.position""",
        (playlist_id,),
    ).fetchall()


def reorder_playlist(con, playlist_id, entry_ids):
    """Rewrite positions to match the given order of playlist_tracks row ids."""
    for position, entry_id in enumerate(entry_ids, start=1):
        con.execute(
            "UPDATE playlist_tracks SET position=? WHERE id=? AND playlist_id=?",
            (position, entry_id, playlist_id),
        )
    con.commit()


def set_favorite(con, track_id, favorite):
    con.execute("UPDATE tracks SET favorite=? WHERE id=?", (1 if favorite else 0, track_id))
    con.commit()


def set_artist_photo(con, artist_id, path):
    con.execute("UPDATE artists SET photo_path=? WHERE id=?", (path, artist_id))
    con.commit()


def set_album_cover(con, album_id, path):
    con.execute("UPDATE albums SET cover_path=? WHERE id=?", (path, album_id))
    con.commit()


def delete_track(con, track_id):
    con.execute("DELETE FROM tracks WHERE id=?", (track_id,))
    con.commit()


def delete_album(con, album_id):
    con.execute("DELETE FROM tracks WHERE album_id=?", (album_id,))
    con.execute("DELETE FROM albums WHERE id=?", (album_id,))
    con.commit()


def delete_artist(con, artist_id):
    con.execute("DELETE FROM tracks WHERE artist_id=?", (artist_id,))
    con.execute("DELETE FROM albums WHERE artist_id=?", (artist_id,))
    con.execute("DELETE FROM artists WHERE id=?", (artist_id,))
    con.commit()
