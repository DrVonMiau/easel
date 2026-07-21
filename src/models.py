from gi.repository import GObject


class Track(GObject.Object):
    __gtype_name__ = "Track"
    id = GObject.Property(type=int, default=0)
    path = GObject.Property(type=str, default="")
    title = GObject.Property(type=str, default="")
    artist = GObject.Property(type=str, default="")
    album = GObject.Property(type=str, default="")
    album_id = GObject.Property(type=int, default=0)
    track_no = GObject.Property(type=int, default=0)
    duration = GObject.Property(type=float, default=0.0)
    favorite = GObject.Property(type=bool, default=False)


class Album(GObject.Object):
    __gtype_name__ = "Album"
    id = GObject.Property(type=int, default=0)
    title = GObject.Property(type=str, default="")
    artist = GObject.Property(type=str, default="")
    year = GObject.Property(type=int, default=0)
    cover_path = GObject.Property(type=str, default="")


class Playlist(GObject.Object):
    __gtype_name__ = "Playlist"
    id = GObject.Property(type=int, default=0)
    name = GObject.Property(type=str, default="")
    track_count = GObject.Property(type=int, default=0)
    cover_path = GObject.Property(type=str, default="")


class Artist(GObject.Object):
    __gtype_name__ = "Artist"
    id = GObject.Property(type=int, default=0)
    name = GObject.Property(type=str, default="")
    photo_path = GObject.Property(type=str, default="")
    album_count = GObject.Property(type=int, default=0)
    track_count = GObject.Property(type=int, default=0)
