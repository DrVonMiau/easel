"""Local photo library: SQLite storage + folder scanner.

An *album* is a folder of photos: every photo's immediate parent directory
becomes its album, and the album's cover is its earliest photo. The index
mirrors the shape of Lyre's music library (folders → albums → items) so the
folder-watching and pruning logic carries over unchanged.
"""
import os
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "easel"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "easel"
COVERS_DIR = CACHE_DIR / "covers"
DB_PATH = DATA_DIR / "library.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders(id INTEGER PRIMARY KEY, path TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS albums(
  id INTEGER PRIMARY KEY, title TEXT, path TEXT UNIQUE, cover_path TEXT);
CREATE TABLE IF NOT EXISTS photos(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE, album_id INTEGER,
  mtime REAL, date_taken REAL, favorite INTEGER DEFAULT 0,
  FOREIGN KEY(album_id) REFERENCES albums(id));
CREATE INDEX IF NOT EXISTS idx_photos_album ON photos(album_id);
"""

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic",
             ".gif", ".tiff", ".tif", ".bmp"}


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


def add_folder(con, path):
    con.execute("INSERT OR IGNORE INTO folders(path) VALUES (?)", (path,))
    con.commit()


def all_folders(con):
    return con.execute("SELECT id, path FROM folders ORDER BY path").fetchall()


def remove_folder(con, path):
    """Forget a folder and every photo scanned from it. Files stay on disk."""
    con.execute("DELETE FROM folders WHERE path=?", (path,))
    con.execute("DELETE FROM photos WHERE path LIKE ?", (path.rstrip("/") + "/%",))
    prune_orphans(con)


def wipe_library(con):
    """Erase the whole library: photos, albums and the folder list. Image
    files on disk are untouched."""
    for table in ("photos", "albums", "folders"):
        con.execute(f"DELETE FROM {table}")
    con.commit()


def get_or_create_album(con, path):
    """The album for a photo is its parent folder. Title is the folder name."""
    row = con.execute("SELECT id FROM albums WHERE path=?", (path,)).fetchone()
    if row:
        return row["id"]
    title = os.path.basename(path.rstrip("/")) or path
    return con.execute(
        "INSERT INTO albums(title, path) VALUES (?,?)", (title, path)
    ).lastrowid


def _date_taken(path):
    """Best-effort capture date. EXIF reading (GExiv2/Pillow) can slot in
    here later; filesystem mtime is a fine placeholder for now."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def scan_file(con, path):
    """Index one image file (insert or update)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return
    existing = con.execute(
        "SELECT id, mtime, album_id FROM photos WHERE path=?", (path,)
    ).fetchone()
    if existing and existing["mtime"] == mtime:
        _maybe_cover(con, existing["album_id"])
        return

    album_id = get_or_create_album(con, os.path.dirname(path))
    date_taken = _date_taken(path)
    if existing:
        con.execute(
            "UPDATE photos SET album_id=?, mtime=?, date_taken=? WHERE id=?",
            (album_id, mtime, date_taken, existing["id"]),
        )
    else:
        con.execute(
            """INSERT INTO photos(path, album_id, mtime, date_taken)
               VALUES (?,?,?,?)""",
            (path, album_id, mtime, date_taken),
        )
    con.commit()
    _maybe_cover(con, album_id)


def _maybe_cover(con, album_id):
    """Give a coverless album its earliest photo as a cover thumbnail."""
    row = con.execute("SELECT cover_path FROM albums WHERE id=?", (album_id,)).fetchone()
    if not row or row["cover_path"]:
        return
    photo = con.execute(
        "SELECT path FROM photos WHERE album_id=? ORDER BY date_taken LIMIT 1",
        (album_id,),
    ).fetchone()
    if photo:
        con.execute("UPDATE albums SET cover_path=? WHERE id=?", (photo["path"], album_id))
        con.commit()


def scan_folder(con, folder, progress_cb=None):
    files = [
        os.path.join(r, f)
        for r, _d, fs in os.walk(folder)
        for f in fs
        if Path(f).suffix.lower() in IMAGE_EXT
    ]
    for i, path in enumerate(files):
        scan_file(con, path)
        if progress_cb:
            progress_cb(i + 1, len(files))
    prune(con, folder)


def prune_orphans(con):
    """Delete albums that no longer hold any photos; refresh stale covers."""
    con.execute("DELETE FROM albums WHERE id NOT IN (SELECT DISTINCT album_id FROM photos)")
    # A cover whose photo was removed should fall back to another photo.
    for row in con.execute(
        """SELECT id FROM albums WHERE cover_path IS NOT NULL
           AND cover_path NOT IN (SELECT path FROM photos)"""
    ).fetchall():
        con.execute("UPDATE albums SET cover_path=NULL WHERE id=?", (row["id"],))
        _maybe_cover(con, row["id"])
    con.commit()


def prune(con, folder):
    for row in con.execute("SELECT id, path FROM photos WHERE path LIKE ?", (folder + "%",)).fetchall():
        if not os.path.exists(row["path"]):
            con.execute("DELETE FROM photos WHERE id=?", (row["id"],))
    prune_orphans(con)


def scan_all(con, progress_cb=None):
    for row in con.execute("SELECT path FROM folders"):
        if os.path.isdir(row["path"]):
            scan_folder(con, row["path"], progress_cb)


# ---------- queries ----------

def all_photos(con):
    return con.execute(
        """SELECT photos.*, albums.title AS album_title FROM photos
           JOIN albums ON albums.id = photos.album_id
           ORDER BY photos.date_taken DESC, photos.path"""
    ).fetchall()


def all_albums(con):
    return con.execute(
        """SELECT albums.*,
             (SELECT COUNT(*) FROM photos WHERE photos.album_id = albums.id) AS photo_count,
             (SELECT MAX(date_taken) FROM photos WHERE photos.album_id = albums.id) AS date_taken
           FROM albums ORDER BY albums.title"""
    ).fetchall()


def photos_by_album(con, album_id):
    return con.execute(
        """SELECT photos.*, albums.title AS album_title FROM photos
           JOIN albums ON albums.id = photos.album_id
           WHERE album_id=? ORDER BY photos.date_taken, photos.path""",
        (album_id,),
    ).fetchall()


def get_photo(con, photo_id):
    return con.execute(
        """SELECT photos.*, albums.title AS album_title FROM photos
           JOIN albums ON albums.id = photos.album_id
           WHERE photos.id=?""",
        (photo_id,),
    ).fetchone()


def get_album(con, album_id):
    return con.execute(
        """SELECT albums.*,
             (SELECT COUNT(*) FROM photos WHERE photos.album_id = albums.id) AS photo_count,
             (SELECT MAX(date_taken) FROM photos WHERE photos.album_id = albums.id) AS date_taken
           FROM albums WHERE albums.id=?""",
        (album_id,),
    ).fetchone()


def set_favorite(con, photo_id, favorite):
    con.execute("UPDATE photos SET favorite=? WHERE id=?", (1 if favorite else 0, photo_id))
    con.commit()


def set_album_cover(con, album_id, path):
    con.execute("UPDATE albums SET cover_path=? WHERE id=?", (path, album_id))
    con.commit()


def delete_photo(con, photo_id):
    con.execute("DELETE FROM photos WHERE id=?", (photo_id,))
    con.commit()
    prune_orphans(con)


def delete_album(con, album_id):
    con.execute("DELETE FROM photos WHERE album_id=?", (album_id,))
    con.execute("DELETE FROM albums WHERE id=?", (album_id,))
    con.commit()
