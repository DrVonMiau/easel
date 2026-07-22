from gi.repository import GObject


class Photo(GObject.Object):
    __gtype_name__ = "Photo"
    id = GObject.Property(type=int, default=0)
    path = GObject.Property(type=str, default="")
    album = GObject.Property(type=str, default="")
    date_taken = GObject.Property(type=float, default=0.0)
    favorite = GObject.Property(type=bool, default=False)
    is_video = GObject.Property(type=bool, default=False)
    rotation = GObject.Property(type=int, default=0)


class Album(GObject.Object):
    """A folder of photos, or a user-created collection."""
    __gtype_name__ = "Album"
    id = GObject.Property(type=int, default=0)
    title = GObject.Property(type=str, default="")
    path = GObject.Property(type=str, default="")
    photo_count = GObject.Property(type=int, default=0)
    cover_path = GObject.Property(type=str, default="")
    date_taken = GObject.Property(type=float, default=0.0)


class Period(GObject.Object):
    """A time bucket (a month or a year) shown as a single summary card in the
    Months/Years views. Keeping one card per period — instead of one tile per
    photo — bounds how many thumbnails a big library ever loads at once."""
    __gtype_name__ = "Period"
    kind = GObject.Property(type=str, default="")   # "month" | "year"
    key = GObject.Property(type=str, default="")     # "2014-03" | "2014"
    title = GObject.Property(type=str, default="")
    subtitle = GObject.Property(type=str, default="")
    cover_path = GObject.Property(type=str, default="")
