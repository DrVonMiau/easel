from gi.repository import GObject


class Photo(GObject.Object):
    __gtype_name__ = "Photo"
    id = GObject.Property(type=int, default=0)
    path = GObject.Property(type=str, default="")
    album = GObject.Property(type=str, default="")
    album_id = GObject.Property(type=int, default=0)
    date_taken = GObject.Property(type=float, default=0.0)
    favorite = GObject.Property(type=bool, default=False)


class Album(GObject.Object):
    """A folder/collection of photos."""
    __gtype_name__ = "Album"
    id = GObject.Property(type=int, default=0)
    title = GObject.Property(type=str, default="")
    path = GObject.Property(type=str, default="")
    photo_count = GObject.Property(type=int, default=0)
    cover_path = GObject.Property(type=str, default="")
    date_taken = GObject.Property(type=float, default=0.0)
