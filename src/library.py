"""Local photo library: SQLite storage + folder scanner.

Photos live in *albums*. Every photo belongs to at least its folder album (the
folder it was scanned from) and can be added to any number of user-created
albums on top of that — so album membership is many-to-many (album_photos).
The folder-watching and pruning shape carries over from Lyre's music library.
"""
import os
import struct
import sqlite3
import time
import zlib
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
  mtime REAL, date_taken REAL, favorite INTEGER DEFAULT 0,
  rotation INTEGER DEFAULT 0,
  lat REAL, lon REAL);
CREATE TABLE IF NOT EXISTS album_photos(
  album_id INTEGER NOT NULL, photo_id INTEGER NOT NULL,
  UNIQUE(album_id, photo_id),
  FOREIGN KEY(album_id) REFERENCES albums(id) ON DELETE CASCADE,
  FOREIGN KEY(photo_id) REFERENCES photos(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_ap_album ON album_photos(album_id);
CREATE INDEX IF NOT EXISTS idx_ap_photo ON album_photos(photo_id);

-- People: manual, hand-made tags (a name pinned onto a photo). No face
-- recognition — every row here was placed by the user. A person can appear at
-- most once per photo; the pin's normalised (x, y) records where the user
-- pointed so it can be shown back on the image.
CREATE TABLE IF NOT EXISTS persons(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS faces(
  id INTEGER PRIMARY KEY,
  photo_id INTEGER NOT NULL, person_id INTEGER NOT NULL,
  x REAL, y REAL,
  UNIQUE(photo_id, person_id),
  FOREIGN KEY(photo_id) REFERENCES photos(id) ON DELETE CASCADE,
  FOREIGN KEY(person_id) REFERENCES persons(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_faces_photo ON faces(photo_id);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);
"""

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif",
             ".gif", ".tiff", ".tif", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi",
             ".3gp", ".mpg", ".mpeg", ".wmv", ".ogv"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT


def is_video(path):
    return Path(path).suffix.lower() in VIDEO_EXT


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    # Migration for libraries created before non-destructive rotation.
    try:
        con.execute("ALTER TABLE photos ADD COLUMN rotation INTEGER DEFAULT 0")
        con.commit()
    except sqlite3.OperationalError:
        pass
    # Migration for libraries created before the Map view (GPS columns). NULL
    # lat/lon means "not read yet"; backfill_locations fills them in lazily.
    for col in ("lat", "lon"):
        try:
            con.execute(f"ALTER TABLE photos ADD COLUMN {col} REAL")
            con.commit()
        except sqlite3.OperationalError:
            pass
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


def in_album(con, album_id, photo_id):
    return con.execute(
        "SELECT 1 FROM album_photos WHERE album_id=? AND photo_id=?", (album_id, photo_id)
    ).fetchone() is not None


# EXIF tags we care about, in the order we prefer them.
_EXIF_DATE_TAGS = (0x9003, 0x9004, 0x0132)  # DateTimeOriginal, DateTimeDigitized, DateTime


def _parse_exif_datetime(value):
    """Turn an EXIF datetime string ('YYYY:MM:DD HH:MM:SS') into a POSIX
    timestamp (interpreted as local time), or None if it doesn't parse."""
    value = value.split("\x00", 1)[0].strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y:%m:%d"):
        try:
            return time.mktime(time.strptime(value, fmt))
        except (ValueError, OverflowError):
            continue
    return None


def _exif_datetime(path):
    """The photo's capture date from its EXIF metadata, as a POSIX timestamp,
    or None if the file has no readable EXIF date. A small self-contained JPEG
    EXIF reader — no gdk-pixbuf/glycin (which fails in the sandbox) and no extra
    dependency."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(256 * 1024)
    except OSError:
        return None
    if head[:2] != b"\xff\xd8":  # not a JPEG
        return None
    # Walk JPEG marker segments to find APP1 (0xFFE1) carrying "Exif\0\0".
    i = 2
    exif = None
    while i + 4 <= len(head):
        if head[i] != 0xFF:
            break
        marker = head[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", head[i + 2:i + 4])[0]
        seg = head[i + 4:i + 2 + seg_len]
        if marker == 0xE1 and seg[:6] == b"Exif\x00\x00":
            exif = seg[6:]
            break
        if marker == 0xDA:  # start of scan — no metadata past here
            break
        i += 2 + seg_len
    if not exif or len(exif) < 8:
        return None
    # TIFF header inside the EXIF block: byte order + magic + IFD0 offset.
    bo = "<" if exif[:2] == b"II" else ">" if exif[:2] == b"MM" else None
    if bo is None:
        return None

    def u16(off):
        return struct.unpack(bo + "H", exif[off:off + 2])[0]

    def u32(off):
        return struct.unpack(bo + "I", exif[off:off + 4])[0]

    def read_ifd(offset, found):
        if offset <= 0 or offset + 2 > len(exif):
            return None
        count = u16(offset)
        entry = offset + 2
        sub_ifd = None
        for _ in range(count):
            if entry + 12 > len(exif):
                break
            tag = u16(entry)
            typ = u16(entry + 2)
            val_off = entry + 8
            if tag in _EXIF_DATE_TAGS and typ == 2:  # ASCII
                n = u32(entry + 4)
                str_off = u32(val_off) if n > 4 else val_off
                if 0 <= str_off <= len(exif):
                    raw = exif[str_off:str_off + min(n, 32)].split(b"\x00", 1)[0]
                    ts = _parse_exif_datetime(raw.decode("ascii", "ignore"))
                    if ts is not None:
                        found[tag] = ts
            elif tag == 0x8769 and typ == 4:  # Exif sub-IFD pointer
                sub_ifd = u32(val_off)
            entry += 12
        return sub_ifd

    found = {}
    sub = read_ifd(u32(4), found)
    if sub is not None:
        read_ifd(sub, found)
    for tag in _EXIF_DATE_TAGS:
        if tag in found:
            return found[tag]
    return None


def _find_exif_tiff(path):
    """Locate a JPEG's EXIF block and return (tiff_bytes, byte_order) where
    tiff_bytes starts at the TIFF header (right after 'Exif\\x00\\x00') and
    byte_order is '<' or '>'. Returns (None, None) if there's no readable EXIF.
    Shared by the GPS reader; the older date reader keeps its own inline walk."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(256 * 1024)
    except OSError:
        return None, None
    if head[:2] != b"\xff\xd8":  # not a JPEG
        return None, None
    i = 2
    exif = None
    while i + 4 <= len(head):
        if head[i] != 0xFF:
            break
        marker = head[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", head[i + 2:i + 4])[0]
        seg = head[i + 4:i + 2 + seg_len]
        if marker == 0xE1 and seg[:6] == b"Exif\x00\x00":
            exif = seg[6:]
            break
        if marker == 0xDA:  # start of scan — no metadata past here
            break
        i += 2 + seg_len
    if not exif or len(exif) < 8:
        return None, None
    bo = "<" if exif[:2] == b"II" else ">" if exif[:2] == b"MM" else None
    if bo is None:
        return None, None
    return exif, bo


def _gps_rationals(exif, bo, off, count):
    """Read `count` EXIF RATIONALs (each two u32: numerator/denominator) starting
    at `off`, as a list of floats. Returns None on any malformed entry."""
    out = []
    for k in range(count):
        base = off + k * 8
        if base + 8 > len(exif):
            return None
        num = struct.unpack(bo + "I", exif[base:base + 4])[0]
        den = struct.unpack(bo + "I", exif[base + 4:base + 8])[0]
        out.append(num / den if den else 0.0)
    return out


def _exif_gps(path):
    """The photo's capture location as (lat, lon) in signed decimal degrees, or
    None if the file carries no readable GPS EXIF. Self-contained, like the date
    reader — no gdk-pixbuf/glycin and no extra dependency.

    Walks IFD0 to the GPS sub-IFD (tag 0x8825) and reads GPSLatitude/Longitude
    (three RATIONALs: degrees, minutes, seconds) with their N/S and E/W refs."""
    exif, bo = _find_exif_tiff(path)
    if exif is None:
        return None

    def u16(o):
        return struct.unpack(bo + "H", exif[o:o + 2])[0]

    def u32(o):
        return struct.unpack(bo + "I", exif[o:o + 4])[0]

    try:
        ifd0 = u32(4)
        if ifd0 <= 0 or ifd0 + 2 > len(exif):
            return None
        # Find the GPS Info IFD pointer (tag 0x8825, LONG) inside IFD0.
        gps_off = None
        count = u16(ifd0)
        entry = ifd0 + 2
        for _ in range(count):
            if entry + 12 > len(exif):
                break
            if u16(entry) == 0x8825:
                gps_off = u32(entry + 8)
                break
            entry += 12
        if not gps_off or gps_off + 2 > len(exif):
            return None
        # Read the GPS IFD: refs (ASCII) and lat/lon (three RATIONALs each).
        lat = lon = None
        lat_ref = lon_ref = None
        count = u16(gps_off)
        entry = gps_off + 2
        for _ in range(count):
            if entry + 12 > len(exif):
                break
            tag = u16(entry)
            typ = u16(entry + 2)
            n = u32(entry + 4)
            val_off = entry + 8
            if tag == 1 and typ == 2:            # GPSLatitudeRef (N/S)
                lat_ref = exif[val_off:val_off + 1].decode("ascii", "ignore")
            elif tag == 3 and typ == 2:          # GPSLongitudeRef (E/W)
                lon_ref = exif[val_off:val_off + 1].decode("ascii", "ignore")
            elif tag == 2 and typ == 5 and n >= 2:   # GPSLatitude
                lat = _gps_rationals(exif, bo, u32(val_off), min(n, 3))
            elif tag == 4 and typ == 5 and n >= 2:   # GPSLongitude
                lon = _gps_rationals(exif, bo, u32(val_off), min(n, 3))
            entry += 12
        if not lat or not lon:
            return None
    except (struct.error, IndexError, ValueError):
        return None

    def to_deg(parts):
        d = parts[0] if len(parts) > 0 else 0.0
        m = parts[1] if len(parts) > 1 else 0.0
        s = parts[2] if len(parts) > 2 else 0.0
        return d + m / 60.0 + s / 3600.0

    latitude = to_deg(lat)
    longitude = to_deg(lon)
    if (lat_ref or "").upper() == "S":
        latitude = -latitude
    if (lon_ref or "").upper() == "W":
        longitude = -longitude
    # Reject obviously bogus fixes (some cameras write 0,0 or out-of-range).
    if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
        return None
    if abs(latitude) < 1e-6 and abs(longitude) < 1e-6:
        return None
    return round(latitude, 6), round(longitude, 6)


def read_exif_segment(path):
    """Return the raw APP1 payload (starting b'Exif\\x00\\x00') of a JPEG, ready
    to be spliced into another JPEG, or None if there's none to copy.

    The photo's on-screen orientation is baked into Easel's edited pixels, so the
    copied metadata's Orientation tag is neutralised to 1 — otherwise a viewer
    would rotate the already-upright copy a second time. Everything else
    (capture date, camera, GPS, …) is preserved verbatim for organisation."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(256 * 1024)
    except OSError:
        return None
    if head[:2] != b"\xff\xd8":  # not a JPEG
        return None
    i = 2
    while i + 4 <= len(head):
        if head[i] != 0xFF:
            break
        marker = head[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", head[i + 2:i + 4])[0]
        seg = head[i + 4:i + 2 + seg_len]
        if marker == 0xE1 and seg[:6] == b"Exif\x00\x00":
            # Only usable if we captured the whole segment.
            if len(seg) != seg_len - 2:
                return None
            return _neutralize_orientation(bytearray(seg))
        if marker == 0xDA:  # start of scan — no metadata past here
            break
        i += 2 + seg_len
    return None


def _neutralize_orientation(seg):
    """Set the EXIF Orientation tag (IFD0 tag 0x0112) to 1 in-place, if present.
    `seg` is the APP1 payload beginning with b'Exif\\x00\\x00'. Returns `seg`."""
    exif = seg[6:]  # skip "Exif\0\0" -> TIFF header
    if len(exif) < 8:
        return seg
    bo = "<" if exif[:2] == b"II" else ">" if exif[:2] == b"MM" else None
    if bo is None:
        return seg
    try:
        ifd0 = struct.unpack(bo + "I", exif[4:8])[0]
        if ifd0 <= 0 or ifd0 + 2 > len(exif):
            return seg
        count = struct.unpack(bo + "H", exif[ifd0:ifd0 + 2])[0]
        entry = ifd0 + 2
        for _ in range(count):
            if entry + 12 > len(exif):
                break
            tag = struct.unpack(bo + "H", exif[entry:entry + 2])[0]
            if tag == 0x0112:  # Orientation (SHORT, inline value at entry+8)
                one = struct.pack(bo + "H", 1)
                # seg = "Exif\0\0" (6 bytes) + exif; value sits at exif[entry+8].
                seg[6 + entry + 8:6 + entry + 10] = one
                break
            entry += 12
    except struct.error:
        pass
    return seg


def exif_tiff_from_segment(seg):
    """Given an APP1 payload (b'Exif\\x00\\x00' + TIFF) as returned by
    read_exif_segment, return just the TIFF/Exif stream — what a PNG eXIf chunk
    stores (the JPEG-only 'Exif\\0\\0' prefix is dropped)."""
    if not seg or bytes(seg[:6]) != b"Exif\x00\x00":
        return None
    return bytes(seg[6:])


def _png_chunk(ctype, payload):
    return (struct.pack(">I", len(payload)) + ctype + payload
            + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF))


def write_png_with_exif(png_path, exif_tiff):
    """Insert an eXIf chunk (standardised PNG Exif container) into an existing
    PNG, right after IHDR, so the edited copy keeps the original's capture
    date/camera/GPS. No-op on any inconsistency so a failed copy never corrupts
    the saved image."""
    if not exif_tiff:
        return
    try:
        with open(png_path, "rb") as fh:
            data = fh.read()
    except OSError:
        return
    sig = b"\x89PNG\r\n\x1a\n"
    if data[:8] != sig or len(data) < 8 + 12:
        return
    ihdr_len = struct.unpack(">I", data[8:12])[0]
    if data[12:16] != b"IHDR":
        return
    ihdr_end = 16 + ihdr_len + 4  # type(already counted) + data + CRC
    chunk = _png_chunk(b"eXIf", bytes(exif_tiff))
    tmp = f"{png_path}.exif.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data[:ihdr_end] + chunk + data[ihdr_end:])
        os.replace(tmp, png_path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _file_created(path):
    """Filesystem creation (birth) time if the platform exposes it, else None."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    bt = getattr(st, "st_birthtime", None)
    return bt if bt else None


def _date_taken(path):
    """Best capture date for a photo: the EXIF DateTimeOriginal if present,
    otherwise the file's creation (birth) time, otherwise its mtime."""
    ts = _exif_datetime(path)
    if ts is not None:
        return ts
    ts = _file_created(path)
    if ts is not None:
        return ts
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
    existing = con.execute(
        "SELECT id, mtime, date_taken FROM photos WHERE path=?", (path,)).fetchone()
    if existing:
        photo_id = existing["id"]
        if existing["mtime"] != mtime:
            con.execute("UPDATE photos SET mtime=? WHERE id=?", (mtime, photo_id))
        # Refresh the capture date from EXIF, but only when the stored date is
        # still the old auto-derived mtime value — never clobber a date the user
        # set by hand.
        if existing["date_taken"] in (None, existing["mtime"]):
            fresh = _date_taken(path)
            if fresh != existing["date_taken"]:
                con.execute("UPDATE photos SET date_taken=? WHERE id=?",
                            (fresh, photo_id))
    else:
        gps = _exif_gps(path)
        lat, lon = gps if gps else (None, None)
        photo_id = con.execute(
            "INSERT INTO photos(path, mtime, date_taken, lat, lon) VALUES (?,?,?,?,?)",
            (path, mtime, _date_taken(path), lat, lon),
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
            if not _is_hidden(f) and Path(f).suffix.lower() in MEDIA_EXT:
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


# ---------- locations (Map view) ----------

def backfill_locations(con, progress_cb=None):
    """Read GPS from any photo whose location hasn't been extracted yet (lat is
    NULL — either never scanned for GPS, or migrated from an older library).
    Returns the number of photos that gained a location. Safe to run in a worker
    thread; commits in one batch at the end.

    Photos with no GPS stay NULL, so a later call re-checks them — cheap unless
    the library is huge, and the Map view only triggers this once per session."""
    rows = con.execute(
        "SELECT id, path FROM photos WHERE lat IS NULL").fetchall()
    total = len(rows)
    found = 0
    for i, row in enumerate(rows):
        gps = _exif_gps(row["path"])
        if gps:
            con.execute("UPDATE photos SET lat=?, lon=? WHERE id=?",
                        (gps[0], gps[1], row["id"]))
            found += 1
        if progress_cb:
            progress_cb(i + 1, total)
    con.commit()
    return found


def photos_with_location(con):
    """Every geotagged photo, newest first — the data the Map view pins."""
    return con.execute(
        f"""SELECT photos.*, {_FOLDER_TITLE} FROM photos
            WHERE lat IS NOT NULL AND lon IS NOT NULL
            ORDER BY photos.date_taken DESC, photos.path"""
    ).fetchall()


# ---------- people (manual tagging) ----------

def get_or_create_person(con, name):
    """The person id for a name, creating the person on first use. Names are
    matched case-insensitively so 'Ada' and 'ada' are the same person, but the
    first spelling the user typed is the one that's stored."""
    name = (name or "").strip()
    if not name:
        return None
    row = con.execute(
        "SELECT id FROM persons WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row["id"]
    return con.execute("INSERT INTO persons(name) VALUES (?)", (name,)).lastrowid


def tag_person(con, photo_id, name, x=0.5, y=0.5):
    """Tag a person into a photo at a normalised (x, y) pin, creating the person
    if new. Re-tagging the same person just moves their pin. Returns the
    person id, or None if the name was blank."""
    person_id = get_or_create_person(con, name)
    if person_id is None:
        return None
    con.execute(
        """INSERT INTO faces(photo_id, person_id, x, y) VALUES (?,?,?,?)
           ON CONFLICT(photo_id, person_id) DO UPDATE SET x=excluded.x, y=excluded.y""",
        (photo_id, person_id, x, y),
    )
    con.commit()
    return person_id


def remove_face(con, photo_id, person_id):
    """Untag a person from a photo. The person themselves is kept even if this
    was their last photo, so their name stays available for re-tagging."""
    con.execute("DELETE FROM faces WHERE photo_id=? AND person_id=?",
                (photo_id, person_id))
    con.commit()


def faces_for_photo(con, photo_id):
    """The people tagged in a photo: rows of (person_id, name, x, y)."""
    return con.execute(
        """SELECT f.person_id, p.name, f.x, f.y FROM faces f
           JOIN persons p ON p.id = f.person_id
           WHERE f.photo_id=? ORDER BY p.name COLLATE NOCASE""",
        (photo_id,),
    ).fetchall()


def all_persons(con):
    """Everyone who's been tagged, with how many photos they're in and a cover
    (their most recent photo). People with no photos left are dropped so the
    People view never shows empty cards."""
    return con.execute(
        """SELECT p.id, p.name,
             (SELECT COUNT(*) FROM faces WHERE faces.person_id = p.id) AS photo_count,
             (SELECT ph.path FROM faces f JOIN photos ph ON ph.id = f.photo_id
              WHERE f.person_id = p.id ORDER BY ph.date_taken DESC LIMIT 1) AS cover_path,
             (SELECT MAX(ph.date_taken) FROM faces f JOIN photos ph ON ph.id = f.photo_id
              WHERE f.person_id = p.id) AS date_taken
           FROM persons p
           WHERE EXISTS (SELECT 1 FROM faces WHERE faces.person_id = p.id)
           ORDER BY p.name COLLATE NOCASE"""
    ).fetchall()


def get_person(con, person_id):
    return con.execute("SELECT id, name FROM persons WHERE id=?",
                       (person_id,)).fetchone()


def photos_for_person(con, person_id):
    """Every photo a person is tagged in, newest first."""
    return con.execute(
        f"""SELECT photos.*, {_FOLDER_TITLE} FROM faces f
            JOIN photos ON photos.id = f.photo_id
            WHERE f.person_id=? ORDER BY photos.date_taken DESC, photos.path""",
        (person_id,),
    ).fetchall()


def rename_person(con, person_id, name):
    """Rename a person, unless the new name collides with someone else."""
    name = (name or "").strip()
    if not name:
        return False
    clash = con.execute(
        "SELECT id FROM persons WHERE name=? COLLATE NOCASE AND id<>?",
        (name, person_id)).fetchone()
    if clash:
        return False
    con.execute("UPDATE persons SET name=? WHERE id=?", (name, person_id))
    con.commit()
    return True


def delete_person(con, person_id):
    """Forget a person entirely; their tags are removed (ON DELETE CASCADE)."""
    con.execute("DELETE FROM persons WHERE id=?", (person_id,))
    con.commit()


def set_rotation(con, photo_id, degrees):
    """Store a non-destructive display rotation (0/90/180/270). The file on disk
    isn't touched; the rotation is applied when the photo is drawn."""
    con.execute("UPDATE photos SET rotation=? WHERE id=?", (degrees % 360, photo_id))
    con.commit()


def set_photo_path(con, photo_id, new_path):
    """Point a library photo at a different file on disk (e.g. an edited copy),
    keeping its favourite / album membership / capture date. Any other row
    already using new_path is dropped first so the UNIQUE(path) holds. Rotation
    resets to 0 — an edited copy has any display rotation baked into its pixels."""
    try:
        mtime = os.path.getmtime(new_path)
    except OSError:
        mtime = 0.0
    con.execute("DELETE FROM photos WHERE path=? AND id<>?", (new_path, photo_id))
    con.execute("UPDATE photos SET path=?, mtime=?, rotation=0 WHERE id=?",
                (new_path, mtime, photo_id))
    con.commit()
    prune_orphans(con)


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
