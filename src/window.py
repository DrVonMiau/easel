"""Easel's main window.

Visual design carries over from Lyre: a tinted desktop, a "paper" card holding
the library (Photos / Albums / Favourites, switched from segmented pill tabs in
the nav band), and a custom titlebar. Instead of a music player, opening a photo
raises a full-window lightbox for large viewing with left/right navigation.
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from . import library as lib
from .models import Album, Photo
from .widgets import Swatch

APP_ID = "io.github.drvonmiau.Easel"

PHOTO_ENTRIES = [
    ("Open", "open"),
    ("Show in Album", "show-album"),
    ("Add to Favourites", "toggle-fav"),
    (None, None),
    ("Set as Album Cover", "set-cover"),
    ("Remove from library", "delete"),
]
ALBUM_ENTRIES = [
    ("Open", "open"),
    (None, None),
    ("Remove from library", "delete"),
]

THEME_SCHEMES = {
    "light": Adw.ColorScheme.FORCE_LIGHT,
    "dark": Adw.ColorScheme.FORCE_DARK,
    "system": Adw.ColorScheme.DEFAULT,
}

VIEW_NAMES = ("photos", "albums", "favourites")

# Fixed spacing scale (px). Documented in the project styleguide.
SPACE_XS, SPACE_S, SPACE_M, SPACE_L, SPACE_XL = 4, 8, 16, 24, 32

# Web-style hand cursor for anything clickable.
POINTER_CURSOR = Gdk.Cursor.new_from_name("pointer")

# Sort options per tab group (favourites shares the photos group).
SORT_OPTIONS = {
    "photos": [("Newest", "date"), ("Name", "name")],
    "albums": [("Name", "name"), ("Newest", "date"), ("Photos", "count")],
}
SORT_GROUP_FOR_TAB = {"photos": "photos", "albums": "albums",
                      "favourites": "photos"}


def _fmt_date(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%-d %b %Y")
    except (ValueError, OSError):
        return ""


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

    middle_stack = Gtk.Template.Child()
    tab_photos = Gtk.Template.Child()
    tab_albums = Gtk.Template.Child()
    tab_favourites = Gtk.Template.Child()
    search_entry = Gtk.Template.Child()

    paper_stack = Gtk.Template.Child()
    photo_grid = Gtk.Template.Child()
    album_grid = Gtk.Template.Child()
    fav_grid = Gtk.Template.Child()

    detail_back_row = Gtk.Template.Child()
    back_btn = Gtk.Template.Child()
    detail_kind_label = Gtk.Template.Child()
    detail_hero_slot = Gtk.Template.Child()
    detail_name_label = Gtk.Template.Child()
    detail_stats_label = Gtk.Template.Child()
    detail_photos_grid = Gtk.Template.Child()

    lightbox_revealer = Gtk.Template.Child()
    lightbox_picture = Gtk.Template.Child()
    lightbox_caption = Gtk.Template.Child()
    lightbox_prev_btn = Gtk.Template.Child()
    lightbox_next_btn = Gtk.Template.Child()
    lightbox_close_btn = Gtk.Template.Child()
    lightbox_fav_btn = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.con = lib.connect()
        self.settings = Gio.Settings.new(APP_ID)

        self.view = "photos"
        self._last_tab = "photos"
        self._detail_album_id = None
        self._detail_hero = None
        self._search_query = ""
        self._photos_all = []
        self._albums_all = []
        self._visible_photos = []
        self._visible_favs = []
        self._detail_photos = []
        self._surface_width = 0
        self._surface_height = 0

        # Lightbox state.
        self._lightbox_photos = []
        self._lightbox_index = 0

        self._sort = {group: self.settings.get_string(f"sort-{group}")
                      for group in SORT_OPTIONS}

        self._tab_buttons = {
            "photos": self.tab_photos,
            "albums": self.tab_albums,
            "favourites": self.tab_favourites,
        }

        self._setup_actions()
        self._setup_window_controls()
        self._setup_lists()
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

    @staticmethod
    def _close_button_is_left(layout):
        """True if the system's decoration layout puts the close button on the
        left half (e.g. "close,minimize,maximize:" as on macOS-style setups)."""
        left = (layout or "").split(":")[0]
        return "close" in left

    def _setup_titlebar_sides(self):
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.connect("notify::gtk-decoration-layout",
                             lambda *_a: self._apply_titlebar_side())
        self._apply_titlebar_side()

    def _apply_titlebar_side(self):
        """Keep the menu button on the OPPOSITE side of the window controls,
        whichever side the system (or a theme switch) puts them."""
        settings = Gtk.Settings.get_default()
        layout = settings.get_property("gtk-decoration-layout") if settings else ""
        box = self.titlebar_box
        if self._close_button_is_left(layout):
            # window buttons on the left -> menu group to the right
            box.reorder_child_after(self.titlebar_spacer, self.wc_start)
            box.reorder_child_after(self.menu_button, self.titlebar_spacer)
        else:
            # window buttons on the right (GNOME default) -> menu stays left
            box.reorder_child_after(self.menu_button, self.wc_start)
            box.reorder_child_after(self.titlebar_spacer, self.menu_button)

    def _apply_pointer_cursors(self):
        """Give every static clickable a hand cursor. Dynamically created
        rows/cards set theirs at creation time. Window controls keep the
        system default on purpose."""
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
        saved_tab = self.settings.get_string("last-tab")
        self._select_tab(saved_tab if saved_tab in VIEW_NAMES else "photos")

    def _on_close_request(self, *_args):
        self.settings.set_boolean("window-maximized", self.is_maximized())
        if not self.is_maximized():
            width, height = self.get_default_size()
            self.settings.set_int("window-width", width)
            self.settings.set_int("window-height", height)
        self.settings.set_string("last-tab",
                                 self._last_tab if self._last_tab in VIEW_NAMES else "photos")
        return False

    # ---------- theme ----------

    def _setup_theme(self):
        style_manager = Adw.StyleManager.get_default()
        style_manager.connect("notify::dark", self._on_dark_changed)
        self._apply_theme(self.settings.get_string("theme"))

    def _apply_theme(self, theme):
        Adw.StyleManager.get_default().set_color_scheme(
            THEME_SCHEMES.get(theme, Adw.ColorScheme.DEFAULT)
        )
        self._on_dark_changed()

    def _on_dark_changed(self, *_args):
        # Our palette is hand-rolled CSS, so mirror libadwaita's dark state
        # as a style class the stylesheet can key its dark overrides off.
        if Adw.StyleManager.get_default().get_dark():
            self.add_css_class("dark")
        else:
            self.remove_css_class("dark")

    # ---------- window chrome ----------

    def _setup_window_controls(self):
        self.search_toggle_btn.connect("toggled", self._on_toggle_search)
        self.search_entry.connect("stop-search", lambda *_: self.search_toggle_btn.set_active(False))

    def _on_toggle_search(self, btn):
        active = btn.get_active()
        self.middle_stack.set_visible_child_name("search" if active else "view")
        if active:
            self.search_entry.grab_focus()
        else:
            self.search_entry.set_text("")

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
        """5% top/left/right margins; the paper is flush to the window bottom.
        On wide windows the margins simply absorb the extra width."""
        width, height = self._surface_width, self._surface_height
        if width <= 0 or height <= 0:
            return
        margin_x = max(SPACE_L, round(width * 0.05))
        self.content_row.set_margin_start(margin_x)
        self.content_row.set_margin_end(margin_x)
        self.content_row.set_margin_top(0)
        self.content_row.set_margin_bottom(0)
        # The nav band always spans exactly the paper.
        self.nav_row.set_margin_start(margin_x)
        self.nav_row.set_margin_end(margin_x)

    def _setup_help_overlay(self):
        builder = Gtk.Builder.new_from_resource("/io/github/drvonmiau/Easel/gtk/help-overlay.ui")
        overlay = builder.get_object("help_overlay")
        if overlay is not None:
            self.set_help_overlay(overlay)

    # ---------- actions ----------

    def _setup_actions(self):
        add_folder = Gio.SimpleAction.new("add-folder", None)
        add_folder.connect("activate", lambda *_a: self._on_add_folder())
        self.add_action(add_folder)

        rescan = Gio.SimpleAction.new("rescan", None)
        rescan.connect("activate", lambda *_a: self._on_rescan())
        self.add_action(rescan)

        preferences = Gio.SimpleAction.new("preferences", None)
        preferences.connect("activate", lambda *_a: self._on_preferences())
        self.add_action(preferences)

        find = Gio.SimpleAction.new("find", None)
        find.connect("activate", lambda *_a: self.search_toggle_btn.set_active(
            not self.search_toggle_btn.get_active()))
        self.add_action(find)

        for i, tab in enumerate(VIEW_NAMES, start=1):
            act = Gio.SimpleAction.new(f"tab-{i}", None)
            act.connect("activate", lambda *_a, t=tab: self._select_tab(t))
            self.add_action(act)

        app = self.get_application()
        if app is not None:
            app.set_accels_for_action("win.find", ["<primary>f"])
            for i in range(1, len(VIEW_NAMES) + 1):
                app.set_accels_for_action(f"win.tab-{i}", [f"<primary>{i}"])

        # Lightbox keyboard: Escape closes, Left/Right navigate. CAPTURE phase
        # so arrows don't get eaten by the grid underneath while it's open.
        key_ctl = Gtk.EventControllerKey()
        key_ctl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctl.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_ctl)

        sort_mode = Gio.SimpleAction.new_stateful(
            "sort-mode", GLib.VariantType.new("s"),
            GLib.Variant("s", self._sort["photos"]))
        sort_mode.connect("activate", self._on_sort_mode)
        self.add_action(sort_mode)

        item_actions = Gio.SimpleActionGroup()
        for name in ("open", "show-album", "set-cover", "toggle-fav", "delete"):
            act = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            act.connect("activate", self._on_item_action)
            item_actions.add_action(act)
        self.insert_action_group("item", item_actions)

    # ---------- list/grid setup ----------

    def _setup_lists(self):
        self.photo_store = Gio.ListStore(item_type=Photo)
        self.photo_grid.set_model(Gtk.NoSelection(model=self.photo_store))
        self.photo_grid.set_factory(self._factory(self._bind_photo_card))
        self.photo_grid.connect(
            "activate", lambda _g, pos: self._open_lightbox(self._visible_photos, pos)
        )

        self.album_store = Gio.ListStore(item_type=Album)
        self.album_grid.set_model(Gtk.SingleSelection(model=self.album_store))
        self.album_grid.set_factory(self._factory(self._bind_album_card))
        self.album_grid.connect(
            "activate", lambda g, p: self._open_album(g.get_model().get_item(p).id)
        )

        self.fav_store = Gio.ListStore(item_type=Photo)
        self.fav_grid.set_model(Gtk.NoSelection(model=self.fav_store))
        self.fav_grid.set_factory(self._factory(self._bind_photo_card))
        self.fav_grid.connect(
            "activate", lambda _g, pos: self._open_lightbox(self._visible_favs, pos)
        )

        self.detail_store = Gio.ListStore(item_type=Photo)
        self.detail_photos_grid.set_model(Gtk.NoSelection(model=self.detail_store))
        self.detail_photos_grid.set_factory(self._factory(self._bind_photo_card))
        self.detail_photos_grid.connect(
            "activate", lambda _g, pos: self._open_lightbox(self._detail_photos, pos)
        )

    def _factory(self, bind_fn):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, item: item.set_child(Gtk.Box()))
        factory.connect("bind", lambda _f, item: bind_fn(item))
        return factory

    def _photo_card_widget(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=160,
                      margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        box.set_cursor(POINTER_CURSOR)
        box.add_css_class("card-box")

        overlay = Gtk.Overlay()
        swatch = Swatch("", size=160)
        swatch.add_css_class("card-swatch")
        overlay.set_child(swatch)

        # Gold star badge, shown on favourited photos (bottom-left).
        star = Gtk.Image(icon_name="starred-symbolic", halign=Gtk.Align.START,
                         valign=Gtk.Align.END, margin_start=8, margin_bottom=8,
                         css_classes=["photo-star"])
        star.set_visible(False)
        overlay.add_overlay(star)

        # Three-dot menu button: hidden until hovered (top-right).
        menu_btn = Gtk.Button(icon_name="easel-more-symbolic", halign=Gtk.Align.END,
                              valign=Gtk.Align.START, margin_top=6, margin_end=6,
                              tooltip_text="More", css_classes=["card-menu-btn"])
        menu_btn.set_visible(False)
        menu_btn.set_cursor(POINTER_CURSOR)
        overlay.add_overlay(menu_btn)

        box.append(overlay)
        box.swatch, box.star, box.menu_btn = swatch, star, menu_btn
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

    def _card_widget(self):
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
                              tooltip_text="More", css_classes=["flat", "card-menu-btn"])
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

    def _bind_photo_card(self, item):
        photo = item.get_item()
        box = item.get_child()
        if not hasattr(box, "swatch"):
            box = self._photo_card_widget()
            item.set_child(box)
        box.swatch.set_path(photo.path or None)
        box.star.set_visible(photo.favorite)
        self._attach_menu(box, "photo", photo.id, PHOTO_ENTRIES)

    def _bind_album_card(self, item):
        album = item.get_item()
        box = item.get_child()
        if not hasattr(box, "swatch"):
            box = self._card_widget()
            item.set_child(box)
        box.swatch.set_placeholder("album")
        box.swatch.set_path(album.cover_path or None)
        box.title.set_label(album.title)
        count = album.photo_count
        box.subtitle.set_label(f"{count} photo{'s' if count != 1 else ''}")
        self._attach_menu(box, "album", album.id, ALBUM_ENTRIES)

    # ---------- context menus ----------

    def _attach_menu(self, widget, kind, item_id, entries, extra=None):
        # Cards get recycled by GridView, so bind() may run many times on the
        # same widget: attach the gesture once, keep its target fresh.
        widget._menu_kind = kind
        widget._menu_item_id = item_id
        widget._menu_entries = entries
        widget._menu_extra = extra or {}
        if getattr(widget, "_easel_menu_attached", False):
            return
        widget._easel_menu_attached = True
        gesture = Gtk.GestureClick(button=3)
        gesture.connect("pressed",
                        lambda _g, _n, x, y: self._show_item_menu(widget, widget, x, y))
        widget.add_controller(gesture)

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
        """Pop the context menu for `widget`, parented to `anchor` at (x, y).
        Returns the popover so callers can react to its close."""
        popover = Gtk.PopoverMenu.new_from_model(self._build_item_menu(widget))
        popover.set_has_arrow(False)
        popover.set_parent(anchor)
        popover.set_pointing_to(Gdk.Rectangle(x=int(x), y=int(y), width=1, height=1))
        # Unparent only AFTER the menu action has dispatched (GTK resolves the
        # clicked item's action after closing the popover).
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()
        return popover

    def _on_item_action(self, action, param):
        data = json.loads(param.get_string())
        kind, item_id, name = data["kind"], data["id"], action.get_name()

        if name == "delete":
            self._confirm_delete(kind, item_id)
            return
        if name == "open":
            if kind == "album":
                self._select_tab("albums")
                self._open_album(item_id)
            else:
                self._open_photo_by_id(item_id)
            return
        if name == "show-album":
            album_id = self._photo_album_id(item_id)
            if album_id:
                self._select_tab("albums")
                self._open_album(album_id)
            return
        if name == "set-cover":
            photo = lib.get_photo(self.con, item_id)
            if photo:
                lib.set_album_cover(self.con, photo["album_id"], photo["path"])
                self._toast("Album cover set")
                self._reload_all()
            return
        if name == "toggle-fav":
            row = lib.get_photo(self.con, item_id)
            if row:
                lib.set_favorite(self.con, item_id, not row["favorite"])
                self._reload_all()
            return

    def _photo_album_id(self, photo_id):
        row = self.con.execute("SELECT album_id FROM photos WHERE id=?", (photo_id,)).fetchone()
        return row["album_id"] if row else None

    def _open_photo_by_id(self, photo_id):
        """Open a photo in the lightbox from whatever list it belongs to now."""
        for source in (self._visible_photos, self._visible_favs, self._detail_photos):
            for i, p in enumerate(source):
                if p.id == photo_id:
                    self._open_lightbox(source, i)
                    return
        row = lib.get_photo(self.con, photo_id)
        if row:
            self._open_lightbox([self._photo_from_row(row)], 0)

    def _confirm_delete(self, kind, item_id):
        if kind == "album":
            heading = "Remove album?"
            body = ("This removes the folder's photos from your library. "
                    "Files on disk are not touched.")
        else:
            heading = "Remove from library?"
            body = "This only removes it from your library. The file on disk is not touched."
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda d, r: self._do_delete(kind, item_id) if r == "remove" else None)
        dialog.present(self)

    def _do_delete(self, kind, item_id):
        {"photo": lib.delete_photo, "album": lib.delete_album}[kind](self.con, item_id)
        if self.view == "detail" and kind == "album" and self._detail_album_id == item_id:
            self._go_back()
        self._reload_all()

    # ---------- lightbox ----------

    def _setup_lightbox(self):
        self.lightbox_close_btn.connect("clicked", lambda *_: self._close_lightbox())
        self.lightbox_prev_btn.connect("clicked", lambda *_: self._lightbox_step(-1))
        self.lightbox_next_btn.connect("clicked", lambda *_: self._lightbox_step(1))
        self.lightbox_fav_btn.connect("clicked", lambda *_: self._lightbox_toggle_fav())
        # A click on the dark backdrop (but not the image/controls) closes it.
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
        self.lightbox_picture.set_filename(photo.path or None)
        name = os.path.basename(photo.path) if photo.path else ""
        date = _fmt_date(photo.date_taken)
        pos = f"{self._lightbox_index + 1} / {len(self._lightbox_photos)}"
        parts = [p for p in (name, date, pos) if p]
        self.lightbox_caption.set_label("   ·   ".join(parts))
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
        # Only close when the release lands on the backdrop box itself, not on
        # the picture or a button bubbling up through it.
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

        folders = Adw.PreferencesGroup(
            title="Photo Folders",
            description="Folders Easel scans for photos",
        )
        for row in lib.all_folders(self.con):
            path = row["path"]
            folder_row = Adw.ActionRow(title=path, title_lines=1)
            remove_btn = Gtk.Button(icon_name="user-trash-symbolic",
                                    valign=Gtk.Align.CENTER,
                                    tooltip_text="Remove folder from library",
                                    css_classes=["flat"])
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
            subtitle="Rescan automatically when files in your photo folders change",
        )
        self.settings.bind("watch-folders", watch_row, "active",
                           Gio.SettingsBindFlags.DEFAULT)
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
                 "Files on disk are not touched.",
        )
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
                 "library. Your photo files on disk are not touched.",
        )
        confirm.add_response("cancel", "Cancel")
        confirm.add_response("delete", "Delete Library")
        confirm.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, response):
            if response != "delete":
                return
            lib.wipe_library(self.con)
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
        """(Re)create directory monitors for every folder in the library.
        Gio monitors aren't recursive, so walk the tree (capped for sanity)."""
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
        # Debounce: file copies fire many events; rescan once things settle.
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
        # An empty library shows the "No Photos Yet" page instead of blank grids.
        if not self._photos_all and name in ("photos", "albums", "favourites"):
            self.paper_stack.set_visible_child_name("empty")
        else:
            self.paper_stack.set_visible_child_name(name)
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
        self._select_tab(self._last_tab if self._last_tab in VIEW_NAMES else "photos")

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

    # ---------- search ----------

    def _on_search_changed(self, entry):
        self._search_query = entry.get_text().strip().lower()
        self._apply_filters()

    def _sorted_photos(self, photos):
        if self._sort["photos"] == "name":
            return sorted(photos, key=lambda p: os.path.basename(p.path).lower())
        return sorted(photos, key=lambda p: -p.date_taken)  # newest first

    def _sorted_albums(self, albums):
        mode = self._sort["albums"]
        if mode == "date":
            return sorted(albums, key=lambda a: -a.date_taken)
        if mode == "count":
            return sorted(albums, key=lambda a: (-a.photo_count, a.title.lower()))
        return sorted(albums, key=lambda a: a.title.lower())

    def _photo_matches(self, p, q):
        return not q or q in os.path.basename(p.path).lower() or q in p.album.lower()

    def _apply_filters(self):
        q = self._search_query

        self._visible_photos = [
            p for p in self._sorted_photos(self._photos_all) if self._photo_matches(p, q)
        ]
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
        """Rescan folders already in the library for new/changed/removed files."""
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
        return Photo(id=r["id"], path=r["path"], album=r["album_title"],
                     album_id=r["album_id"], date_taken=r["date_taken"] or 0.0,
                     favorite=bool(r["favorite"]))

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
            self._select_tab(self.view)  # refreshes the empty-state page
        return False
