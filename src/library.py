"""Local photo library: SQLite storage + folder scanner.

Photos live in *albums*. Every photo belongs to at least its folder album (the
folder it was scanned from) and can be added to any number of user-created
albums on top of that — so album membership is many-to-many (album_photos).
The folder-watching and pruning shape carries over from Lyre's music library.
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
  id INTEGER PRIMARY KEY, title TEXT, path TEXT UNIQUE,
  cover_path TEXT, user_created INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS photos(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE,
  mtime REAL, date_taken REAL, favorite INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS album_photos(
  album_id INTEGER NOT NULL, photo_id INTEGER NOT NULL,
  UNIQUE(album_id, photo_id),
  FOREIGN KEY(album_id) REFERENCES albums(id) ON DELETE CASCADE,
  FOREIGN KEY(photo_id) REFERENCES photos(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_ap_album ON album_photos(album_id);
CREATE INDEX IF NOT EXISTS idx_ap_photo ON album_photos(photo_id);
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
    # One-off cleanup for libraries scanned before hidden files were skipped:
    # drop any indexed dotfile / AppleDouble sidecar (a "/." anywhere in the
    # path means a hidden path component). Cascades to album_photos.
    con.execute("DELETE FROM photos WHERE path LIKE '%/.%'")
    con.commit()
    prune_orphans(con)
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
    """Erase the whole library. Image files on disk are untouched."""
    for table in ("album_photos", "photos", "albums", "folders"):
        con.execute(f"DELETE FROM {table}")
    con.commit()


# ---------- albums ----------

def get_or_create_folder_album(con, path):
    """The intrinsic album for a photo's folder; title is the folder name."""
    row = con.execute("SELECT id FROM albums WHERE path=?", (path,)).fetchone()
    if row:
        return row["id"]
    title = os.path.basename(path.rstrip("/")) or path
    return con.execute(
        "INSERT INTO albums(title, path, user_created) VALUES (?,?,0)", (title, path)
    ).lastrowid


def create_album(con, title):
    """A user-created album (no folder on disk backs it)."""
    album_id = con.execute(
        "INSERT INTO albums(title, path, user_created) VALUES (?,NULL,1)", (title,)
    ).lastrowid
    con.commit()
    return album_id


def add_to_album(con, album_id, photo_ids):
    for photo_id in photo_ids:
        con.execute(
            "INSERT OR IGNORE INTO album_photos(album_id, photo_id) VALUES (?,?)",
            (album_id, photo_id),
        )
    con.commit()
    _maybe_cover(con, album_id)


def remove_from_album(con, album_id, photo_id):
    con.execute(
        "DELETE FROM album_photos WHERE album_id=? AND photo_id=?", (album_id, photo_id)
    )
    con.commit()


def _date_taken(path):
    """Best-effort capture date. EXIF reading (GExiv2/Pillow) can slot in
    here later; filesystem mtime is a fine placeholder for now."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def scan_file(con, path):
    """Index one image file (insert or update) and file it under its folder."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return
    album_id = get_or_create_folder_album(con, os.path.dirname(path))
    existing = con.execute("SELECT id, mtime FROM photos WHERE path=?", (path,)).fetchone()
    if existing:
        photo_id = existing["id"]
        if existing["mtime"] != mtime:
            con.execute("UPDATE photos SET mtime=? WHERE id=?", (mtime, photo_id))
    else:
        photo_id = con.execute(
            "INSERT INTO photos(path, mtime, date_taken) VALUES (?,?,?)",
            (path, mtime, _date_taken(path)),
        ).lastrowid
    con.execute(
        "INSERT OR IGNORE INTO album_photos(album_id, photo_id) VALUES (?,?)",
        (album_id, photo_id),
    )
    con.commit()
    _maybe_cover(con, album_id)


def _maybe_cover(con, album_id):
    """Give a coverless album its earliest photo as a cover thumbnail."""
    row = con.execute("SELECT cover_path FROM albums WHERE id=?", (album_id,)).fetchone()
    if not row or row["cover_path"]:
        return
    photo = con.execute(
        """SELECT p.path FROM album_photos ap JOIN photos p ON p.id = ap.photo_id
           WHERE ap.album_id=? ORDER BY p.date_taken LIMIT 1""",
        (album_id,),
    ).fetchone()
    if photo:
        con.execute("UPDATE albums SET cover_path=? WHERE id=?", (photo["path"], album_id))
        con.commit()


def _is_hidden(name):
    # Skip dotfiles, including macOS AppleDouble sidecars (._Foo.jpg) that
    # carry an image extension but aren't real images.
    return name.startswith(".")


def scan_folder(con, folder, progress_cb=None):
    files = []
    for root, dirs, fs in os.walk(folder):
        dirs[:] = [d for d in dirs if not _is_hidden(d)]  # don't descend hidden dirs
        for f in fs:
            if not _is_hidden(f) and Path(f).suffix.lower() in IMAGE_EXT:
                files.append(os.path.join(root, f))
    for i, path in enumerate(files):
        scan_file(con, path)
        if progress_cb:
            progress_cb(i + 1, len(files))
    prune(con, folder)


def prune_orphans(con):
    """Delete empty *folder* albums (user albums are kept even when empty) and
    refresh any cover whose photo has gone."""
    con.execute(
        """DELETE FROM albums WHERE user_created=0
           AND id NOT IN (SELECT DISTINCT album_id FROM album_photos)"""
    )
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

_FOLDER_TITLE = """(SELECT a.title FROM album_photos ap JOIN albums a ON a.id = ap.album_id
   WHERE ap.photo_id = photos.id AND a.path IS NOT NULL LIMIT 1) AS album_title"""


def all_photos(con):
    return con.execute(
        f"""SELECT photos.*, {_FOLDER_TITLE} FROM photos
            ORDER BY photos.date_taken DESC, photos.path"""
    ).fetchall()


def all_albums(con):
    return con.execute(
        """SELECT albums.*,
             (SELECT COUNT(*) FROM album_photos WHERE album_photos.album_id = albums.id) AS photo_count,
             (SELECT MAX(p.date_taken) FROM album_photos ap JOIN photos p ON p.id = ap.photo_id
              WHERE ap.album_id = albums.id) AS date_taken
           FROM albums ORDER BY user_created DESC, albums.title"""
    ).fetchall()


def photos_by_album(con, album_id):
    return con.execute(
        f"""SELECT photos.*, {_FOLDER_TITLE} FROM album_photos ap
            JOIN photos ON photos.id = ap.photo_id
            WHERE ap.album_id=? ORDER BY photos.date_taken, photos.path""",
        (album_id,),
    ).fetchall()


def get_photo(con, photo_id):
    return con.execute(
        f"""SELECT photos.*, {_FOLDER_TITLE} FROM photos WHERE photos.id=?""",
        (photo_id,),
    ).fetchone()


def get_album(con, album_id):
    return con.execute(
        """SELECT albums.*,
             (SELECT COUNT(*) FROM album_photos WHERE album_photos.album_id = albums.id) AS photo_count,
             (SELECT MAX(p.date_taken) FROM album_photos ap JOIN photos p ON p.id = ap.photo_id
              WHERE ap.album_id = albums.id) AS date_taken
           FROM albums WHERE albums.id=?""",
        (album_id,),
    ).fetchone()


def set_favorite(con, photo_id, favorite):
    con.execute("UPDATE photos SET favorite=? WHERE id=?", (1 if favorite else 0, photo_id))
    con.commit()


def set_photo_date(con, photo_id, date_taken):
    """Correct a photo's capture date. Stored in the library; sorting and the
    Months/Days views follow it immediately. (Writing it back into the file's
    EXIF is a later addition.)"""
    con.execute("UPDATE photos SET date_taken=? WHERE id=?", (date_taken, photo_id))
    con.commit()


def set_album_cover(con, album_id, path):
    con.execute("UPDATE albums SET cover_path=? WHERE id=?", (path, album_id))
    con.commit()


def rename_album(con, album_id, title):
    con.execute("UPDATE albums SET title=? WHERE id=?", (title, album_id))
    con.commit()


def delete_photo(con, photo_id):
    con.execute("DELETE FROM photos WHERE id=?", (photo_id,))
    con.commit()
    prune_orphans(con)


def delete_album(con, album_id):
    """Remove an album. Folder albums also drop their photos from the library;
    user albums just disband (the photos stay everywhere else they live)."""
    row = con.execute("SELECT user_created FROM albums WHERE id=?", (album_id,)).fetchone()
    if row and not row["user_created"]:
        con.execute(
            """DELETE FROM photos WHERE id IN
               (SELECT photo_id FROM album_photos WHERE album_id=?)""",
            (album_id,),
        )
    con.execute("DELETE FROM albums WHERE id=?", (album_id,))
    con.commit()
    prune_orphans(con)
