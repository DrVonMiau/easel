"""Easel's main window.

Visual design carries over from Lyre: a tinted desktop, a "paper" card holding
the library, segmented pill tabs and a custom titlebar. The music player is
replaced by two photo surfaces — a slide-in info panel (single click) and a
full-window lightbox (double click) — and the volume slider becomes a
thumbnail-size slider.

Tabs are two groups: the primary time views (All Photos / Months / Days) and a
secondary group (Albums / Favourites / Maps / People). Maps and People are
placeholders for now — they need EXIF GPS and face detection respectively.
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from . import library as lib
from .models import Album, Photo
from .widgets import Swatch, load_thumbnail

APP_ID = "io.github.drvonmiau.Easel"

PHOTO_ENTRIES = [
    ("Edit Image…", "edit-image"),
    ("Add to", "__albums__"),
    ("Edit Info…", "edit-info"),
    ("Add to Favourites", "toggle-fav"),
    (None, None),
    ("Set as Album Cover", "set-cover"),
    ("Delete Picture", "delete"),
]
ALBUM_ENTRIES = [
    ("Open", "open"),
    ("Rename…", "rename-album"),
    (None, None),
    ("Delete album", "delete"),
]

THEME_SCHEMES = {
    "light": Adw.ColorScheme.FORCE_LIGHT,
    "dark": Adw.ColorScheme.FORCE_DARK,
    "system": Adw.ColorScheme.DEFAULT,
}

# Primary (time) tabs then the secondary group; order matches the accelerators.
VIEW_NAMES = ("all_photos", "months", "days", "albums", "favourites", "maps", "people")

SPACE_XS, SPACE_S, SPACE_M, SPACE_L, SPACE_XL = 4, 8, 16, 24, 32

POINTER_CURSOR = Gdk.Cursor.new_from_name("pointer")

SORT_OPTIONS = {
    "photos": [("Newest", "date"), ("Name", "name")],
    "albums": [("Name", "name"), ("Newest", "date"), ("Photos", "count")],
}
SORT_GROUP_FOR_TAB = {"all_photos": "photos", "favourites": "photos", "albums": "albums"}


def _fmt_date(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%-d %b %Y")
    except (ValueError, OSError):
        return ""


def _fmt_size(nbytes):
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024 or unit == "GB":
            return f"{nbytes:.0f} {unit}" if unit == "B" else f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return ""


def _dimensions(path):
    """(width, height) without decoding the whole image, or None."""
    try:
        info = GdkPixbuf.Pixbuf.get_file_info(path)
    except Exception:
        return None
    if not info or info[0] is None:
        return None
    return info[1], info[2]


@Gtk.Template(resource_path="/io/github/drvonmiau/Easel/window.ui")
class EaselWindow(Adw.ApplicationWindow):
    __gtype_name__ = "EaselWindow"

    toast_overlay = Gtk.Template.Child()
    root_box = Gtk.Template.Child()
    content_row = Gtk.Template.Child()
    search_toggle_btn = Gtk.Template.Child()
    sort_btn = Gtk.Template.Child()
    nav_row = Gtk.Template.Child()
    titlebar_box = Gtk.Template.Child()
    titlebar_spacer = Gtk.Template.Child()
    wc_start = Gtk.Template.Child()
    wc_end = Gtk.Template.Child()
    menu_button = Gtk.Template.Child()
    thumb_scale = Gtk.Template.Child()

    middle_stack = Gtk.Template.Child()
    tab_all_photos = Gtk.Template.Child()
    tab_months = Gtk.Template.Child()
    tab_days = Gtk.Template.Child()
    tab_albums = Gtk.Template.Child()
    tab_favourites = Gtk.Template.Child()
    tab_maps = Gtk.Template.Child()
    tab_people = Gtk.Template.Child()
    search_entry = Gtk.Template.Child()

    paper_stack = Gtk.Template.Child()
    photo_grid = Gtk.Template.Child()
    months_box = Gtk.Template.Child()
    days_box = Gtk.Template.Child()
    album_grid = Gtk.Template.Child()
    fav_grid = Gtk.Template.Child()

    detail_back_row = Gtk.Template.Child()
    back_btn = Gtk.Template.Child()
    detail_kind_label = Gtk.Template.Child()
    detail_hero_slot = Gtk.Template.Child()
    detail_name_label = Gtk.Template.Child()
    detail_stats_label = Gtk.Template.Child()
    detail_photos_grid = Gtk.Template.Child()

    info_revealer = Gtk.Template.Child()
    info_panel = Gtk.Template.Child()
    info_preview_slot = Gtk.Template.Child()
    info_title = Gtk.Template.Child()
    info_rows_box = Gtk.Template.Child()
    info_close_btn = Gtk.Template.Child()
    info_fav_btn = Gtk.Template.Child()
    info_fullscreen_btn = Gtk.Template.Child()

    lightbox_revealer = Gtk.Template.Child()
    lightbox_picture = Gtk.Template.Child()
    lightbox_caption = Gtk.Template.Child()
    lightbox_prev_btn = Gtk.Template.Child()
    lightbox_next_btn = Gtk.Template.Child()
    lightbox_close_btn = Gtk.Template.Child()
    lightbox_fav_btn = Gtk.Template.Child()

    PANEL_WIDTH = 320

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.con = lib.connect()
        self.settings = Gio.Settings.new(APP_ID)

        self.view = "all_photos"
        self._last_tab = "all_photos"
        self._detail_album_id = None
        self._search_query = ""
        self._photos_all = []
        self._albums_all = []
        self._visible_photos = []
        self._visible_favs = []
        self._detail_photos = []
        self._surface_width = 0
        self._surface_height = 0
        self._thumb_size = self.settings.get_int("thumb-size")
        self._info_photo_id = None
        self._info_preview = None
        self._single_click_source = 0

        self._lightbox_photos = []
        self._lightbox_index = 0

        self._sort = {group: self.settings.get_string(f"sort-{group}")
                      for group in SORT_OPTIONS}

        self._tab_buttons = {
            "all_photos": self.tab_all_photos,
            "months": self.tab_months,
            "days": self.tab_days,
            "albums": self.tab_albums,
            "favourites": self.tab_favourites,
            "maps": self.tab_maps,
            "people": self.tab_people,
        }

        self._setup_actions()
        self._setup_window_controls()
        self._setup_lists()
        self._setup_info_panel()
        self._setup_lightbox()
        self._setup_help_overlay()

        for key, btn in self._tab_buttons.items():
            btn.connect("clicked", lambda _b, k=key: self._select_tab(k))
        self.back_btn.connect("clicked", lambda *_: self._go_back())
        self.search_entry.connect("search-changed", self._on_search_changed)

        self.connect("realize", self._on_realize)
        self.connect("close-request", self._on_close_request)

        self._setup_theme()
        self._restore_state()
        self._reload_all()
        self._setup_watching()
        self._setup_titlebar_sides()
        self._apply_pointer_cursors()

    # ---------- titlebar sides ----------

    @staticmethod
    def _close_button_is_left(layout):
        left = (layout or "").split(":")[0]
        return "close" in left

    def _setup_titlebar_sides(self):
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.connect("notify::gtk-decoration-layout",
                             lambda *_a: self._apply_titlebar_side())
        self._apply_titlebar_side()

    def _apply_titlebar_side(self):
        """Keep the thumb-size + menu group opposite the window controls."""
        settings = Gtk.Settings.get_default()
        layout = settings.get_property("gtk-decoration-layout") if settings else ""
        box = self.titlebar_box
        aux = (self.thumb_scale, self.menu_button)
        if self._close_button_is_left(layout):
            box.reorder_child_after(self.titlebar_spacer, self.wc_start)
            previous = self.titlebar_spacer
        else:
            previous = self.wc_start
        for widget in aux:
            box.reorder_child_after(widget, previous)
            previous = widget
        if not self._close_button_is_left(layout):
            box.reorder_child_after(self.titlebar_spacer, previous)

    def _apply_pointer_cursors(self):
        def walk(widget):
            if isinstance(widget, Gtk.WindowControls):
                return
            if isinstance(widget, (Gtk.Button, Gtk.Scale)):
                widget.set_cursor(POINTER_CURSOR)
            child = widget.get_first_child()
            while child:
                walk(child)
                child = child.get_next_sibling()
        walk(self)

    # ---------- remembered state ----------

    def _restore_state(self):
        self.set_default_size(self.settings.get_int("window-width"),
                              self.settings.get_int("window-height"))
        if self.settings.get_boolean("window-maximized"):
            self.maximize()
        self.thumb_scale.set_value(self._thumb_size)
        saved_tab = self.settings.get_string("last-tab")
        self._select_tab(saved_tab if saved_tab in VIEW_NAMES else "all_photos")

    def _on_close_request(self, *_args):
        self.settings.set_boolean("window-maximized", self.is_maximized())
        if not self.is_maximized():
            width, height = self.get_default_size()
            self.settings.set_int("window-width", width)
            self.settings.set_int("window-height", height)
        self.settings.set_string("last-tab",
                                 self._last_tab if self._last_tab in VIEW_NAMES else "all_photos")
        return False

    # ---------- theme ----------

    def _setup_theme(self):
        Adw.StyleManager.get_default().connect("notify::dark", self._on_dark_changed)
        self._apply_theme(self.settings.get_string("theme"))

    def _apply_theme(self, theme):
        Adw.StyleManager.get_default().set_color_scheme(
            THEME_SCHEMES.get(theme, Adw.ColorScheme.DEFAULT))
        self._on_dark_changed()

    def _on_dark_changed(self, *_args):
        if Adw.StyleManager.get_default().get_dark():
            self.add_css_class("dark")
        else:
            self.remove_css_class("dark")

    # ---------- window chrome ----------

    def _setup_window_controls(self):
        self.search_toggle_btn.connect("toggled", self._on_toggle_search)
        self.search_entry.connect("stop-search", lambda *_: self.search_toggle_btn.set_active(False))
        self.thumb_scale.connect("value-changed", self._on_thumb_changed)

    def _on_toggle_search(self, btn):
        active = btn.get_active()
        self.middle_stack.set_visible_child_name("search" if active else "view")
        if active:
            self.search_entry.grab_focus()
        else:
            self.search_entry.set_text("")

    def _on_thumb_changed(self, scale):
        val = int(scale.get_value())
        if val == self._thumb_size:
            return
        self._thumb_size = val
        self.settings.set_int("thumb-size", val)
        self._relayout_thumbs()

    def _relayout_thumbs(self):
        # Grids rebind from their stores (bind reads the current thumb size);
        # the grouped views are rebuilt only if currently shown.
        self._apply_filters()
        if self.view == "months":
            self._render_months()
        elif self.view == "days":
            self._render_days()
        elif self.view == "detail":
            self._render_detail()

    def _on_realize(self, *_args):
        surface = self.get_surface()
        if surface is not None:
            surface.connect("notify::width", self._on_surface_resize)
            surface.connect("notify::height", self._on_surface_resize)
            self._on_surface_resize(surface, None)

    def _on_surface_resize(self, surface, _pspec):
        self._surface_width = surface.get_width()
        self._surface_height = surface.get_height()
        self._apply_layout_metrics()
        return False

    def _apply_layout_metrics(self):
        """5% margins; when the info panel is revealed it takes a fixed width on
        the right and the paper shrinks to make room (as Lyre's player did)."""
        width, height = self._surface_width, self._surface_height
        if width <= 0 or height <= 0:
            return
        margin_y = round(height * 0.05)
        margin_x = max(SPACE_L, round(width * 0.05))
        revealed = self.info_revealer.get_reveal_child()
        if revealed:
            gap = round(width * 0.04)
            ideal_paper = round(width * 0.62)
            centered = (width - ideal_paper - gap - self.PANEL_WIDTH) // 2
            margin_x = max(margin_x, centered)
        else:
            gap = 0
        self.content_row.set_margin_start(margin_x)
        self.content_row.set_margin_end(margin_x)
        self.content_row.set_margin_top(0)
        self.content_row.set_margin_bottom(0)
        self.nav_row.set_margin_start(margin_x)
        self.nav_row.set_margin_end(margin_x + (gap + self.PANEL_WIDTH if revealed else 0))
        self.info_panel.set_size_request(self.PANEL_WIDTH if revealed else 0, -1)
        self.info_revealer.set_margin_start(gap)
        self.info_revealer.set_margin_bottom(margin_y)

    def _setup_help_overlay(self):
        builder = Gtk.Builder.new_from_resource("/io/github/drvonmiau/Easel/gtk/help-overlay.ui")
        overlay = builder.get_object("help_overlay")
        if overlay is not None:
            self.set_help_overlay(overlay)

    # ---------- actions ----------

    def _setup_actions(self):
        for name, handler in (
            ("add-folder", lambda *_a: self._on_add_folder()),
            ("rescan", lambda *_a: self._on_rescan()),
            ("new-album", lambda *_a: self._on_new_album()),
            ("preferences", lambda *_a: self._on_preferences()),
            ("find", lambda *_a: self.search_toggle_btn.set_active(
                not self.search_toggle_btn.get_active())),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", handler)
            self.add_action(act)

        for i, tab in enumerate(VIEW_NAMES, start=1):
            act = Gio.SimpleAction.new(f"tab-{i}", None)
            act.connect("activate", lambda *_a, t=tab: self._select_tab(t))
            self.add_action(act)

        app = self.get_application()
        if app is not None:
            app.set_accels_for_action("win.find", ["<primary>f"])
            for i in range(1, len(VIEW_NAMES) + 1):
                app.set_accels_for_action(f"win.tab-{i}", [f"<primary>{i}"])

        key_ctl = Gtk.EventControllerKey()
        key_ctl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctl.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_ctl)

        sort_mode = Gio.SimpleAction.new_stateful(
            "sort-mode", GLib.VariantType.new("s"), GLib.Variant("s", self._sort["photos"]))
        sort_mode.connect("activate", self._on_sort_mode)
        self.add_action(sort_mode)

        item_actions = Gio.SimpleActionGroup()
        for name in ("open", "edit-image", "edit-info", "add-to-album",
                     "add-to-new-album", "set-cover", "toggle-fav",
                     "rename-album", "delete"):
            act = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            act.connect("activate", self._on_item_action)
            item_actions.add_action(act)
        self.insert_action_group("item", item_actions)

    # ---------- tiles ----------

    def _setup_lists(self):
        self.photo_store = Gio.ListStore(item_type=Photo)
        self.photo_grid.set_model(Gtk.NoSelection(model=self.photo_store))
        self.photo_grid.set_factory(self._factory(lambda it: self._bind_tile_item(it, "photos")))

        self.album_store = Gio.ListStore(item_type=Album)
        self.album_grid.set_model(Gtk.SingleSelection(model=self.album_store))
        self.album_grid.set_factory(self._factory(self._bind_album_card))
        self.album_grid.set_single_click_activate(True)
        self.album_grid.connect(
            "activate", lambda g, p: self._open_album(g.get_model().get_item(p).id))

        self.fav_store = Gio.ListStore(item_type=Photo)
        self.fav_grid.set_model(Gtk.NoSelection(model=self.fav_store))
        self.fav_grid.set_factory(self._factory(lambda it: self._bind_tile_item(it, "favourites")))

        self.detail_store = Gio.ListStore(item_type=Photo)
        self.detail_photos_grid.set_model(Gtk.NoSelection(model=self.detail_store))
        self.detail_photos_grid.set_factory(self._factory(lambda it: self._bind_tile_item(it, "detail")))

    def _factory(self, bind_fn):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, item: item.set_child(Gtk.Box()))
        factory.connect("bind", lambda _f, item: bind_fn(item))
        return factory

    def _source_for(self, name):
        return {"photos": self._visible_photos, "favourites": self._visible_favs,
                "detail": self._detail_photos}.get(name, self._visible_photos)

    def _bind_tile_item(self, item, source_name):
        photo = item.get_item()
        tile = item.get_child()
        if not hasattr(tile, "swatch"):
            tile = self._make_tile()
            item.set_child(tile)
        self._bind_tile(tile, photo, source_name)

    def _make_tile(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                      margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        box.set_cursor(POINTER_CURSOR)
        box.add_css_class("card-box")

        overlay = Gtk.Overlay()
        swatch = Swatch("", size=self._thumb_size)
        swatch.add_css_class("card-swatch")
        overlay.set_child(swatch)

        heart = Gtk.Button(icon_name="non-starred-symbolic", halign=Gtk.Align.START,
                           valign=Gtk.Align.START, margin_top=6, margin_start=6,
                           tooltip_text="Favourite", css_classes=["tile-heart"])
        heart.set_visible(False)
        heart.set_cursor(POINTER_CURSOR)
        heart.connect("clicked", lambda _b: self._toggle_fav(box._photo.id))
        overlay.add_overlay(heart)

        star = Gtk.Image(icon_name="starred-symbolic", halign=Gtk.Align.START,
                         valign=Gtk.Align.END, margin_start=8, margin_bottom=8,
                         css_classes=["photo-star"])
        star.set_visible(False)
        overlay.add_overlay(star)

        menu_btn = Gtk.Button(icon_name="easel-more-symbolic", halign=Gtk.Align.END,
                              valign=Gtk.Align.START, margin_top=6, margin_end=6,
                              tooltip_text="More", css_classes=["card-menu-btn"])
        menu_btn.set_visible(False)
        menu_btn.set_cursor(POINTER_CURSOR)
        overlay.add_overlay(menu_btn)

        box.append(overlay)
        box.swatch, box.heart, box.star, box.menu_btn = swatch, heart, star, menu_btn
        box._photo = None
        box._source = "photos"
        box._menu_open = False

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_a: (box.heart.set_visible(True),
                                             box.menu_btn.set_visible(True)))
        motion.connect("leave", lambda *_a: self._tile_unhover(box))
        box.add_controller(motion)
        box._motion = motion

        def on_menu_clicked(btn):
            box._menu_open = True
            popover = self._show_item_menu(box, btn, btn.get_width() / 2, btn.get_height())
            popover.connect("closed", lambda _p: self._tile_menu_closed(box))

        menu_btn.connect("clicked", on_menu_clicked)

        left = Gtk.GestureClick(button=1)
        left.connect("pressed", lambda _g, n, x, y: self._tile_pressed(n, box))
        box.add_controller(left)

        right = Gtk.GestureClick(button=3)
        right.connect("pressed", lambda _g, _n, x, y: self._show_item_menu(box, box, x, y))
        box.add_controller(right)

        box.set_has_tooltip(True)
        box.connect("query-tooltip", self._on_tile_tooltip)
        return box

    def _tile_unhover(self, box):
        box.heart.set_visible(False)
        if not box._menu_open:
            box.menu_btn.set_visible(False)

    def _tile_menu_closed(self, box):
        box._menu_open = False
        if not box._motion.get_contains_pointer():
            box.menu_btn.set_visible(False)
            box.heart.set_visible(False)

    def _bind_tile(self, tile, photo, source_name):
        tile._photo = photo
        tile._source = source_name
        tile._menu_kind = "photo"
        tile._menu_item_id = photo.id
        tile._menu_entries = PHOTO_ENTRIES
        tile._menu_extra = {}
        tile.swatch.set_size(self._thumb_size)
        tile.set_size_request(self._thumb_size + 12, -1)
        tile.swatch.set_path(photo.path or None)
        tile.star.set_visible(photo.favorite)

    def _tile_pressed(self, n_press, tile):
        photo = tile._photo
        if photo is None:
            return
        if n_press >= 2:
            if self._single_click_source:
                GLib.source_remove(self._single_click_source)
                self._single_click_source = 0
            source = self._source_for(tile._source)
            ids = [p.id for p in source]
            index = ids.index(photo.id) if photo.id in ids else 0
            self._open_lightbox(source if source else [photo], index)
        elif n_press == 1:
            if self._single_click_source:
                GLib.source_remove(self._single_click_source)
            pid = photo.id
            self._single_click_source = GLib.timeout_add(230, lambda: self._single_fire(pid))

    def _single_fire(self, photo_id):
        self._single_click_source = 0
        self._show_info(photo_id)
        return False

    def _on_tile_tooltip(self, widget, _x, _y, _keyboard, tooltip):
        photo = getattr(widget, "_photo", None)
        if photo is None:
            return False
        tooltip.set_markup(self._tooltip_markup(photo))
        return True

    def _tooltip_markup(self, photo):
        esc = GLib.markup_escape_text
        lines = [f"<b>{esc(os.path.basename(photo.path))}</b>"]
        meta = []
        if photo.album:
            meta.append(photo.album)
        date = _fmt_date(photo.date_taken)
        if date:
            meta.append(date)
        if meta:
            lines.append(esc(" · ".join(meta)))
        dims = _dimensions(photo.path)
        try:
            size = _fmt_size(os.path.getsize(photo.path))
        except OSError:
            size = ""
        tail = " · ".join(x for x in ((f"{dims[0]}×{dims[1]}" if dims else ""), size) if x)
        if tail:
            lines.append(esc(tail))
        return "\n".join(lines)

    def _bind_album_card(self, item):
        album = item.get_item()
        box = item.get_child()
        if not hasattr(box, "swatch"):
            box = self._album_card_widget()
            item.set_child(box)
        box.swatch.set_placeholder("album")
        box.swatch.set_path(album.cover_path or None)
        box.title.set_label(album.title)
        count = album.photo_count
        box.subtitle.set_label(f"{count} photo{'s' if count != 1 else ''}")
        self._attach_album_menu(box, album.id)

    def _album_card_widget(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=192,
                      margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        box.set_cursor(POINTER_CURSOR)
        box.add_css_class("card-box")
        swatch = Swatch("", size=192)
        swatch.add_css_class("card-swatch")

        text_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["card-title"])
        subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["mono-dim-sm"])
        text_col.append(title)
        text_col.append(subtitle)

        menu_btn = Gtk.Button(icon_name="easel-more-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="More", css_classes=["flat", "card-menu-btn-flat"])
        menu_btn.set_visible(False)
        menu_btn.set_cursor(POINTER_CURSOR)
        text_row.append(text_col)
        text_row.append(menu_btn)

        box.append(swatch)
        box.append(text_row)
        box.swatch, box.title, box.subtitle, box.menu_btn = swatch, title, subtitle, menu_btn
        box._menu_open = False

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_a: box.menu_btn.set_visible(True))
        motion.connect("leave",
                       lambda *_a: None if box._menu_open else box.menu_btn.set_visible(False))
        box.add_controller(motion)
        box._motion = motion

        def on_menu_clicked(btn):
            box._menu_open = True
            popover = self._show_item_menu(box, btn, btn.get_width() / 2, btn.get_height())

            def on_closed(_p):
                box._menu_open = False
                if not box._motion.get_contains_pointer():
                    box.menu_btn.set_visible(False)

            popover.connect("closed", on_closed)

        menu_btn.connect("clicked", on_menu_clicked)
        return box

    def _attach_album_menu(self, box, album_id):
        box._menu_kind = "album"
        box._menu_item_id = album_id
        box._menu_entries = ALBUM_ENTRIES
        box._menu_extra = {}
        if getattr(box, "_album_menu_attached", False):
            return
        box._album_menu_attached = True
        gesture = Gtk.GestureClick(button=3)
        gesture.connect("pressed", lambda _g, _n, x, y: self._show_item_menu(box, box, x, y))
        box.add_controller(gesture)

    # ---------- grouped views (Months / Days) ----------

    def _render_months(self):
        self._render_grouped(self.months_box, "%Y-%m", "%B %Y")

    def _render_days(self):
        self._render_grouped(self.days_box, "%Y-%m-%d", "%A, %-d %B %Y")

    def _render_grouped(self, container, key_fmt, label_fmt):
        self._clear_box(container)
        photos = sorted(self._visible_photos, key=lambda p: -p.date_taken)
        groups = []
        index = {}
        for p in photos:
            try:
                dt = datetime.fromtimestamp(p.date_taken)
                key, label = dt.strftime(key_fmt), dt.strftime(label_fmt)
            except (ValueError, OSError):
                key, label = "unknown", "Undated"
            if key not in index:
                index[key] = len(groups)
                groups.append((label, []))
            groups[index[key]][1].append(p)
        for label, items in groups:
            section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            header = Gtk.Label(label=label, xalign=0, css_classes=["group-header"])
            section.append(header)
            flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                               row_spacing=6, column_spacing=6, halign=Gtk.Align.FILL,
                               max_children_per_line=30)
            for p in items:
                tile = self._make_tile()
                self._bind_tile(tile, p, "photos")
                flow.append(tile)
            section.append(flow)
            container.append(section)

    # ---------- context menus ----------

    def _build_item_menu(self, widget):
        def payload(**more):
            data = {"kind": widget._menu_kind, "id": widget._menu_item_id}
            data.update(widget._menu_extra)
            data.update(more)
            return GLib.Variant("s", json.dumps(data))

        menu = Gio.Menu()
        section = Gio.Menu()
        for label, action in widget._menu_entries:
            if label is None:
                menu.append_section(None, section)
                section = Gio.Menu()
                continue
            if action == "__albums__":
                sub = Gio.Menu()
                for album in self._albums_all:
                    mi = Gio.MenuItem.new(album.title, None)
                    mi.set_action_and_target_value("item.add-to-album", payload(album=album.id))
                    sub.append_item(mi)
                mi = Gio.MenuItem.new("New Album…", None)
                mi.set_action_and_target_value("item.add-to-new-album", payload())
                sub.append_item(mi)
                section.append_submenu(label, sub)
                continue
            if action == "toggle-fav":
                row = lib.get_photo(self.con, widget._menu_item_id)
                label = ("Remove from Favourites" if row and row["favorite"]
                         else "Add to Favourites")
            mi = Gio.MenuItem.new(label, None)
            mi.set_action_and_target_value(f"item.{action}", payload())
            section.append_item(mi)
        menu.append_section(None, section)
        return menu

    def _show_item_menu(self, widget, anchor, x, y):
        popover = Gtk.PopoverMenu.new_from_model(self._build_item_menu(widget))
        popover.set_has_arrow(False)
        popover.set_parent(anchor)
        popover.set_pointing_to(Gdk.Rectangle(x=int(x), y=int(y), width=1, height=1))
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()
        return popover

    def _on_item_action(self, action, param):
        data = json.loads(param.get_string())
        kind, item_id, name = data["kind"], data["id"], action.get_name()

        if name == "delete":
            self._confirm_delete(kind, item_id)
            return
        if name == "open" and kind == "album":
            self._select_tab("albums")
            self._open_album(item_id)
            return
        if name == "edit-image":
            self._toast("Image editing is coming soon")
            return
        if name == "edit-info":
            self._edit_info(item_id)
            return
        if name == "set-cover":
            photo = lib.get_photo(self.con, item_id)
            albums = self.con.execute(
                """SELECT a.id FROM album_photos ap JOIN albums a ON a.id = ap.album_id
                   WHERE ap.photo_id=? AND a.path IS NOT NULL LIMIT 1""", (item_id,)).fetchone()
            if photo and albums:
                lib.set_album_cover(self.con, albums["id"], photo["path"])
                self._toast("Album cover set")
                self._reload_all()
            return
        if name == "toggle-fav":
            self._toggle_fav(item_id)
            return
        if name == "add-to-album":
            lib.add_to_album(self.con, data["album"], [item_id])
            album = lib.get_album(self.con, data["album"])
            self._toast(f'Added to "{album["title"]}"' if album else "Added to album")
            self._reload_all()
            return
        if name == "add-to-new-album":
            self._prompt_name("New Album", "", lambda text: (
                lib.add_to_album(self.con, lib.create_album(self.con, text), [item_id]),
                self._toast(f'Added to "{text}"'), self._reload_all()))
            return
        if name == "rename-album":
            album = lib.get_album(self.con, item_id)
            if album:
                self._prompt_name("Rename Album", album["title"], lambda text: (
                    lib.rename_album(self.con, item_id, text), self._reload_all()))
            return

    def _toggle_fav(self, photo_id):
        row = lib.get_photo(self.con, photo_id)
        if not row:
            return
        lib.set_favorite(self.con, photo_id, not row["favorite"])
        self._reload_all()
        if self._info_photo_id == photo_id:
            self._refresh_info_fav()

    def _confirm_delete(self, kind, item_id):
        if kind == "album":
            album = lib.get_album(self.con, item_id)
            if album and album["user_created"]:
                heading, body = "Delete album?", "This deletes the album. The photos stay in your library."
            else:
                heading = "Remove album?"
                body = ("This removes the folder's photos from your library. "
                        "Files on disk are not touched.")
        else:
            heading = "Delete picture?"
            body = "This only removes it from your library. The file on disk is not touched."
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda d, r: self._do_delete(kind, item_id) if r == "remove" else None)
        dialog.present(self)

    def _do_delete(self, kind, item_id):
        {"photo": lib.delete_photo, "album": lib.delete_album}[kind](self.con, item_id)
        if kind == "photo" and self._info_photo_id == item_id:
            self._close_info()
        if self.view == "detail" and kind == "album" and self._detail_album_id == item_id:
            self._go_back()
        self._reload_all()

    def _prompt_name(self, heading, initial, on_accept):
        entry = Gtk.Entry(text=initial, activates_default=True, margin_top=6)
        dialog = Adw.AlertDialog(heading=heading, extra_child=entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("accept", "Save")
        dialog.set_response_appearance("accept", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("accept")

        def on_response(_d, response):
            text = entry.get_text().strip()
            if response == "accept" and text:
                on_accept(text)

        dialog.connect("response", on_response)
        dialog.present(self)
        entry.grab_focus()

    def _edit_info(self, photo_id):
        """Edit a photo's stored capture date — the fix for photos that came
        back from a device with the wrong year. Stored in the library for now
        (EXIF write-back is a later addition)."""
        row = lib.get_photo(self.con, photo_id)
        if not row:
            return
        ts = row["date_taken"] or 0.0
        try:
            current = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
        except (ValueError, OSError):
            current = ""
        fields = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                             css_classes=["boxed-list"], margin_top=8)
        date_row = Adw.EntryRow(title="Date taken (YYYY-MM-DD)", text=current)
        fields.append(date_row)

        dialog = Adw.AlertDialog(heading="Edit Info", body=os.path.basename(row["path"]),
                                 extra_child=fields)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def on_response(_d, response):
            if response != "save":
                return
            text = date_row.get_text().strip()
            try:
                dt = datetime.strptime(text, "%Y-%m-%d")
            except ValueError:
                self._toast("Enter a date as YYYY-MM-DD")
                return
            # Keep the existing time-of-day so only the date shifts.
            if ts:
                old = datetime.fromtimestamp(ts)
                dt = dt.replace(hour=old.hour, minute=old.minute, second=old.second)
            lib.set_photo_date(self.con, photo_id, dt.timestamp())
            self._reload_all()
            if self._info_photo_id == photo_id:
                self._show_info(photo_id)
            self._toast("Date updated")

        dialog.connect("response", on_response)
        dialog.present(self)

    # ---------- info panel ----------

    def _setup_info_panel(self):
        self.info_close_btn.connect("clicked", lambda *_: self._close_info())
        self.info_fav_btn.connect("clicked", lambda *_: self._toggle_fav(self._info_photo_id)
                                  if self._info_photo_id else None)
        self.info_fullscreen_btn.connect("clicked", lambda *_: self._info_fullscreen())

    def _show_info(self, photo_id):
        row = lib.get_photo(self.con, photo_id)
        if not row:
            return
        self._info_photo_id = photo_id
        if self._info_preview is None:
            self._info_preview = Gtk.Picture(content_fit=Gtk.ContentFit.COVER)
            self._info_preview.add_css_class("info-preview")
            self._info_preview.set_size_request(-1, 220)
            self.info_preview_slot.append(self._info_preview)
        self._info_preview.set_paintable(load_thumbnail(row["path"], 480))
        self.info_title.set_label(os.path.basename(row["path"]))

        self._clear_box(self.info_rows_box)
        dims = _dimensions(row["path"])
        try:
            size = _fmt_size(os.path.getsize(row["path"]))
        except OSError:
            size = "—"
        pairs = [
            ("Album", row["album_title"] or "—"),
            ("Date", _fmt_date(row["date_taken"]) or "Undated"),
            ("Dimensions", f"{dims[0]} × {dims[1]}" if dims else "—"),
            ("Size", size),
            ("Path", row["path"]),
        ]
        for key, value in pairs:
            self.info_rows_box.append(self._info_row(key, value))

        self._refresh_info_fav()
        self.info_revealer.set_visible(True)
        self.info_revealer.set_reveal_child(True)
        self._apply_layout_metrics()

    def _info_row(self, key, value):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        k = Gtk.Label(label=key.upper(), xalign=0, css_classes=["info-key"])
        v = Gtk.Label(label=value, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                      selectable=True, css_classes=["info-value"])
        row.append(k)
        row.append(v)
        return row

    def _refresh_info_fav(self):
        if self._info_photo_id is None:
            return
        row = lib.get_photo(self.con, self._info_photo_id)
        fav = bool(row["favorite"]) if row else False
        self.info_fav_btn.set_icon_name("starred-symbolic" if fav else "non-starred-symbolic")
        if fav:
            self.info_fav_btn.add_css_class("faved")
        else:
            self.info_fav_btn.remove_css_class("faved")

    def _close_info(self):
        self.info_revealer.set_reveal_child(False)
        self.info_revealer.set_visible(False)
        self._info_photo_id = None
        self._apply_layout_metrics()

    def _info_fullscreen(self):
        if self._info_photo_id is not None:
            self._open_photo_by_id(self._info_photo_id)

    # ---------- lightbox ----------

    def _setup_lightbox(self):
        self.lightbox_close_btn.connect("clicked", lambda *_: self._close_lightbox())
        self.lightbox_prev_btn.connect("clicked", lambda *_: self._lightbox_step(-1))
        self.lightbox_next_btn.connect("clicked", lambda *_: self._lightbox_step(1))
        self.lightbox_fav_btn.connect("clicked", lambda *_: self._lightbox_toggle_fav())
        click = Gtk.GestureClick(button=1)
        click.connect("released", self._on_lightbox_backdrop)
        self.lightbox_picture.get_parent().add_controller(click)

    def _lightbox_visible(self):
        return self.lightbox_revealer.get_visible()

    def _open_lightbox(self, photos, index):
        if not photos or not (0 <= index < len(photos)):
            return
        self._lightbox_photos = list(photos)
        self._lightbox_index = index
        self.lightbox_revealer.set_visible(True)
        self.lightbox_revealer.set_reveal_child(True)
        self._show_lightbox_photo()

    def _open_photo_by_id(self, photo_id):
        for source in (self._visible_photos, self._visible_favs, self._detail_photos):
            for i, p in enumerate(source):
                if p.id == photo_id:
                    self._open_lightbox(source, i)
                    return
        row = lib.get_photo(self.con, photo_id)
        if row:
            self._open_lightbox([self._photo_from_row(row)], 0)

    def _close_lightbox(self):
        self.lightbox_revealer.set_reveal_child(False)
        self.lightbox_revealer.set_visible(False)

    def _lightbox_step(self, delta):
        if not self._lightbox_photos:
            return
        self._lightbox_index = (self._lightbox_index + delta) % len(self._lightbox_photos)
        self._show_lightbox_photo()

    def _show_lightbox_photo(self):
        photo = self._lightbox_photos[self._lightbox_index]
        # Full resolution here (one image at a time) — this is the detail view.
        try:
            self.lightbox_picture.set_filename(photo.path or None)
        except Exception:
            self.lightbox_picture.set_paintable(None)
        name = os.path.basename(photo.path) if photo.path else ""
        date = _fmt_date(photo.date_taken)
        pos = f"{self._lightbox_index + 1} / {len(self._lightbox_photos)}"
        self.lightbox_caption.set_label("   ·   ".join(p for p in (name, date, pos) if p))
        multi = len(self._lightbox_photos) > 1
        self.lightbox_prev_btn.set_visible(multi)
        self.lightbox_next_btn.set_visible(multi)
        self._update_lightbox_fav(photo)

    def _update_lightbox_fav(self, photo):
        row = lib.get_photo(self.con, photo.id)
        fav = bool(row["favorite"]) if row else photo.favorite
        self.lightbox_fav_btn.set_icon_name("starred-symbolic" if fav else "non-starred-symbolic")
        if fav:
            self.lightbox_fav_btn.add_css_class("faved")
        else:
            self.lightbox_fav_btn.remove_css_class("faved")

    def _lightbox_toggle_fav(self):
        if not self._lightbox_photos:
            return
        photo = self._lightbox_photos[self._lightbox_index]
        row = lib.get_photo(self.con, photo.id)
        if not row:
            return
        new_fav = not row["favorite"]
        lib.set_favorite(self.con, photo.id, new_fav)
        photo.favorite = new_fav
        self._update_lightbox_fav(photo)
        self._reload_all()

    def _on_lightbox_backdrop(self, gesture, _n, x, y):
        widget = gesture.get_widget()
        picked = widget.pick(x, y, Gtk.PickFlags.DEFAULT)
        if picked is widget or picked is None:
            self._close_lightbox()

    def _on_key_pressed(self, _ctl, keyval, _keycode, _state):
        if not self._lightbox_visible():
            return False
        if keyval == Gdk.KEY_Escape:
            self._close_lightbox()
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Up):
            self._lightbox_step(-1)
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Down, Gdk.KEY_space):
            self._lightbox_step(1)
            return True
        return False

    # ---------- preferences / folder watching ----------

    def _on_new_album(self):
        self._prompt_name("New Album", "", lambda text: (
            lib.create_album(self.con, text), self._select_tab("albums"), self._reload_all()))

    def _on_preferences(self):
        dialog = Adw.PreferencesDialog(title="Preferences")
        page = Adw.PreferencesPage()

        appearance = Adw.PreferencesGroup(title="Appearance")
        themes = ("light", "dark", "system")
        theme_row = Adw.ComboRow(title="Theme",
                                 model=Gtk.StringList.new(["Light", "Dark", "System"]))
        current = self.settings.get_string("theme")
        theme_row.set_selected(themes.index(current) if current in themes else 2)

        def on_theme_selected(row, _pspec):
            theme = themes[row.get_selected()]
            self.settings.set_string("theme", theme)
            self._apply_theme(theme)

        theme_row.connect("notify::selected", on_theme_selected)
        appearance.add(theme_row)
        page.add(appearance)

        folders = Adw.PreferencesGroup(title="Photo Folders",
                                       description="Folders Easel scans for photos")
        for row in lib.all_folders(self.con):
            path = row["path"]
            folder_row = Adw.ActionRow(title=path, title_lines=1)
            remove_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                    tooltip_text="Remove folder from library", css_classes=["flat"])
            remove_btn.connect("clicked",
                               lambda _b, p=path, d=dialog: self._confirm_remove_folder(p, d))
            folder_row.add_suffix(remove_btn)
            folders.add(folder_row)
        add_row = Adw.ActionRow(title="Add Photo Folder…", activatable=True)
        add_row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        add_row.connect("activated", lambda *_: (dialog.close(), self._on_add_folder()))
        folders.add(add_row)
        watch_row = Adw.SwitchRow(
            title="Watch photo folders",
            subtitle="Rescan automatically when files in your photo folders change")
        self.settings.bind("watch-folders", watch_row, "active", Gio.SettingsBindFlags.DEFAULT)
        folders.add(watch_row)
        page.add(folders)

        danger = Adw.PreferencesGroup(title="Reset")
        delete_row = Adw.ActionRow(title="Delete Library…", activatable=True)
        delete_row.add_css_class("error")
        delete_row.connect("activated", lambda *_: self._confirm_wipe_library(dialog))
        danger.add(delete_row)
        page.add(danger)

        dialog.add(page)
        dialog.present(self)

    def _confirm_remove_folder(self, path, prefs_dialog):
        confirm = Adw.AlertDialog(
            heading="Remove folder?",
            body=f"Photos from “{path}” will be removed from your library. "
                 "Files on disk are not touched.")
        confirm.add_response("cancel", "Cancel")
        confirm.add_response("remove", "Remove")
        confirm.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, response):
            if response != "remove":
                return
            lib.remove_folder(self.con, path)
            self._reload_all()
            self._refresh_watchers()
            self._toast("Folder removed")
            prefs_dialog.close()

        confirm.connect("response", on_response)
        confirm.present(self)

    def _confirm_wipe_library(self, prefs_dialog):
        confirm = Adw.AlertDialog(
            heading="Delete entire library?",
            body="All albums, photos and favourites will be erased from the "
                 "library. Your photo files on disk are not touched.")
        confirm.add_response("cancel", "Cancel")
        confirm.add_response("delete", "Delete Library")
        confirm.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, response):
            if response != "delete":
                return
            lib.wipe_library(self.con)
            self._close_info()
            self._reload_all()
            self._refresh_watchers()
            self._toast("Library deleted")
            prefs_dialog.close()

        confirm.connect("response", on_response)
        confirm.present(self)

    def _setup_watching(self):
        self._monitors = []
        self._watch_debounce = 0
        self.settings.connect("changed::watch-folders", lambda *_: self._refresh_watchers())
        self._refresh_watchers()

    def _refresh_watchers(self):
        for monitor in self._monitors:
            monitor.cancel()
        self._monitors = []
        if not self.settings.get_boolean("watch-folders"):
            return
        count = 0
        for row in self.con.execute("SELECT path FROM folders").fetchall():
            for dirpath, _dirs, _files in os.walk(row["path"]):
                if count >= 512:
                    return
                try:
                    monitor = Gio.File.new_for_path(dirpath).monitor_directory(
                        Gio.FileMonitorFlags.NONE, None)
                except GLib.Error:
                    continue
                monitor.connect("changed", self._on_folder_event)
                self._monitors.append(monitor)
                count += 1

    def _on_folder_event(self, *_args):
        if self._watch_debounce:
            GLib.source_remove(self._watch_debounce)
        self._watch_debounce = GLib.timeout_add_seconds(3, self._watch_rescan)

    def _watch_rescan(self):
        self._watch_debounce = 0

        def work():
            lib.scan_all(self.con)
            GLib.idle_add(self._reload_all)
            GLib.idle_add(self._refresh_watchers)
            GLib.idle_add(self._toast_photo_count)

        threading.Thread(target=work, daemon=True).start()
        return False

    # ---------- tabs / navigation ----------

    def _toast(self, text):
        self.toast_overlay.add_toast(Adw.Toast.new(text))

    def _on_sort_mode(self, action, param):
        group = SORT_GROUP_FOR_TAB.get(self.view)
        if not group:
            return
        mode = param.get_string()
        action.set_state(param)
        self._sort[group] = mode
        self.settings.set_string(f"sort-{group}", mode)
        self._apply_filters()

    def _update_sort_button(self):
        group = SORT_GROUP_FOR_TAB.get(self.view)
        self.sort_btn.set_visible(group is not None)
        if group is None:
            return
        menu = Gio.Menu()
        section = Gio.Menu()
        for label, mode in SORT_OPTIONS[group]:
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value("win.sort-mode", GLib.Variant("s", mode))
            section.append_item(item)
        menu.append_section("Sort by", section)
        self.sort_btn.set_menu_model(menu)
        action = self.lookup_action("sort-mode")
        if action:
            action.set_state(GLib.Variant("s", self._sort[group]))

    def _select_tab(self, name):
        self.view = name
        self._last_tab = name
        if not self._photos_all and name in ("all_photos", "months", "days", "favourites"):
            self.paper_stack.set_visible_child_name("empty")
        else:
            self.paper_stack.set_visible_child_name(name)
            if name == "months":
                self._render_months()
            elif name == "days":
                self._render_days()
        self.detail_back_row.set_visible(False)
        self._update_sort_button()
        for key, btn in self._tab_buttons.items():
            if key == name:
                btn.add_css_class("tab-active")
            else:
                btn.remove_css_class("tab-active")

    def _open_album(self, album_id):
        album = lib.get_album(self.con, album_id)
        if not album:
            return
        self.view = "detail"
        self._detail_album_id = album_id
        self.paper_stack.set_visible_child_name("detail")
        self.detail_back_row.set_visible(True)
        self.sort_btn.set_visible(False)
        self._render_detail()

    def _go_back(self):
        self._select_tab(self._last_tab if self._last_tab in VIEW_NAMES else "all_photos")

    def _clear_box(self, box):
        child = box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    def _render_detail(self):
        album = lib.get_album(self.con, self._detail_album_id)
        if not album:
            self._go_back()
            return
        self.detail_kind_label.set_label("Album")
        self._clear_box(self.detail_hero_slot)
        hero = Swatch("album", size=108)
        hero.set_path(album["cover_path"] or None)
        self.detail_hero_slot.append(hero)
        self.detail_name_label.set_label(album["title"])

        rows = lib.photos_by_album(self.con, album["id"])
        count = len(rows)
        parts = [f"{count} photo{'s' if count != 1 else ''}"]
        date = _fmt_date(album["date_taken"])
        if date:
            parts.append(date)
        self.detail_stats_label.set_label(" · ".join(parts))

        self._detail_photos = [self._photo_from_row(r) for r in rows]
        self.detail_store.remove_all()
        for p in self._detail_photos:
            self.detail_store.append(p)

    # ---------- search / filters ----------

    def _on_search_changed(self, entry):
        self._search_query = entry.get_text().strip().lower()
        self._apply_filters()

    def _sorted_photos(self, photos):
        if self._sort["photos"] == "name":
            return sorted(photos, key=lambda p: os.path.basename(p.path).lower())
        return sorted(photos, key=lambda p: -p.date_taken)

    def _sorted_albums(self, albums):
        mode = self._sort["albums"]
        if mode == "date":
            return sorted(albums, key=lambda a: -a.date_taken)
        if mode == "count":
            return sorted(albums, key=lambda a: (-a.photo_count, a.title.lower()))
        return sorted(albums, key=lambda a: a.title.lower())

    def _photo_matches(self, p, q):
        return not q or q in os.path.basename(p.path).lower() or q in (p.album or "").lower()

    def _apply_filters(self):
        q = self._search_query

        self._visible_photos = [p for p in self._sorted_photos(self._photos_all)
                                if self._photo_matches(p, q)]
        self.photo_store.remove_all()
        for p in self._visible_photos:
            self.photo_store.append(p)

        self.album_store.remove_all()
        for a in self._sorted_albums(self._albums_all):
            if not q or q in a.title.lower():
                self.album_store.append(a)

        self._visible_favs = [p for p in self._visible_photos if p.favorite]
        self.fav_store.remove_all()
        for p in self._visible_favs:
            self.fav_store.append(p)

    # ---------- library loading ----------

    def _on_add_folder(self):
        dialog = Gtk.FileDialog()
        dialog.select_folder(self, None, self._folder_chosen)

    def _on_rescan(self):
        self._toast("Rescanning library…")

        def work():
            lib.scan_all(self.con)
            GLib.idle_add(self._reload_all)
            GLib.idle_add(self._toast_photo_count)

        threading.Thread(target=work, daemon=True).start()

    def _toast_photo_count(self):
        self._toast(f"Library updated — {len(self._photos_all)} photos")
        return False

    def _folder_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if not folder:
            return
        path = folder.get_path()
        lib.add_folder(self.con, path)
        self._toast("Scanning folder…")

        def work():
            lib.scan_folder(self.con, path)
            GLib.idle_add(self._reload_all)
            GLib.idle_add(self._refresh_watchers)
            GLib.idle_add(self._toast_photo_count)

        threading.Thread(target=work, daemon=True).start()

    def _photo_from_row(self, r):
        return Photo(id=r["id"], path=r["path"], album=r["album_title"] or "",
                     date_taken=r["date_taken"] or 0.0, favorite=bool(r["favorite"]))

    def _reload_all(self):
        self._photos_all = [self._photo_from_row(r) for r in lib.all_photos(self.con)]
        self._albums_all = [
            Album(id=r["id"], title=r["title"], path=r["path"] or "",
                  photo_count=r["photo_count"] or 0, cover_path=r["cover_path"] or "",
                  date_taken=r["date_taken"] or 0.0)
            for r in lib.all_albums(self.con)
        ]
        self._apply_filters()
        if self.view == "detail" and self._detail_album_id is not None:
            self._render_detail()
        elif self.view in VIEW_NAMES:
            self._select_tab(self.view)
        return False
