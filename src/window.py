"""Easel's main window.

Visual design carries over from Lyre: a tinted desktop, a "paper" card holding
the library, segmented pill tabs and a custom titlebar. The music player is
replaced by two photo surfaces — a slide-in info panel (single click) and a
full-window lightbox (double click) — and the volume slider becomes a
thumbnail-size slider.

Tabs are two groups: the primary time views (All Photos / Months / Years) and a
secondary group (Albums / Favourites / Map / People). Months and Years show
bounded period cards that drill into a recycling grid, so a big library never
loads a tile per photo. Map pins each geotagged photo on an offline world map;
People gathers photos by the names the user has tagged into them by hand.
"""
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Graphene, Gtk, Pango

from . import library as lib
from .models import Album, Period, Person, Photo
from .widgets import (AdjustableImage, AdjustScale, CropOverlay, FacePinLayer,
                      FilterThumb, MapView, Swatch, load_full_texture,
                      render_adjusted_texture)

APP_ID = "io.github.drvonmiau.Easel"

PHOTO_ENTRIES = [
    ("Edit Image…", "edit-image"),
    ("Add to", "__albums__"),
    ("Edit Info…", "edit-info"),
    ("Add to Favourites", "toggle-fav"),
    (None, None),
    ("Set as Album Cover", "set-cover"),
    ("Hide", "hide"),
    ("Move to Trash…", "trash"),
]
ALBUM_ENTRIES = [
    ("Open", "open"),
    ("Rename…", "rename-album"),
    (None, None),
    ("Delete album", "delete"),
]
PERSON_ENTRIES = [
    ("Open", "open"),
    ("Rename…", "rename-person"),
    (None, None),
    ("Remove Person", "delete"),
]

THEME_SCHEMES = {
    "light": Adw.ColorScheme.FORCE_LIGHT,
    "dark": Adw.ColorScheme.FORCE_DARK,
    "system": Adw.ColorScheme.DEFAULT,
}

# Primary (time) tabs then the secondary group; order matches the accelerators.
VIEW_NAMES = ("all_photos", "months", "years", "people", "map", "albums", "favourites")

SPACE_XS, SPACE_S, SPACE_M, SPACE_L, SPACE_XL = 4, 8, 16, 24, 32

POINTER_CURSOR = Gdk.Cursor.new_from_name("pointer")

SORT_OPTIONS = {
    "photos": [("Newest", "date"), ("Oldest", "date-asc")],
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


class _MenuTarget:
    """A lightweight stand-in carrying the _menu_* attributes _build_item_menu
    reads, for context menus not anchored to a bound tile (e.g. the info
    panel's Add button)."""

    def __init__(self, kind, item_id, entries, extra=None):
        self._menu_kind = kind
        self._menu_item_id = item_id
        self._menu_entries = entries
        self._menu_extra = extra or {}


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
    tab_years = Gtk.Template.Child()
    tab_albums = Gtk.Template.Child()
    tab_favourites = Gtk.Template.Child()
    tab_map = Gtk.Template.Child()
    tab_people = Gtk.Template.Child()
    search_entry = Gtk.Template.Child()

    paper_stack = Gtk.Template.Child()
    photo_grid = Gtk.Template.Child()
    months_grid = Gtk.Template.Child()
    years_grid = Gtk.Template.Child()
    album_grid = Gtk.Template.Child()
    fav_grid = Gtk.Template.Child()
    map_stack = Gtk.Template.Child()
    map_slot = Gtk.Template.Child()
    people_stack = Gtk.Template.Child()
    people_grid = Gtk.Template.Child()

    detail_back_row = Gtk.Template.Child()
    back_btn = Gtk.Template.Child()
    detail_kind_label = Gtk.Template.Child()
    detail_hero_slot = Gtk.Template.Child()
    detail_name_label = Gtk.Template.Child()
    detail_stats_label = Gtk.Template.Child()
    detail_folders_grid = Gtk.Template.Child()
    detail_photos_grid = Gtk.Template.Child()

    info_revealer = Gtk.Template.Child()
    info_panel = Gtk.Template.Child()
    info_preview_slot = Gtk.Template.Child()
    info_rows_box = Gtk.Template.Child()
    info_close_btn = Gtk.Template.Child()
    info_fullscreen_btn = Gtk.Template.Child()
    info_rotate_left_btn = Gtk.Template.Child()
    info_rotate_right_btn = Gtk.Template.Child()
    info_add_btn = Gtk.Template.Child()
    info_edit_btn = Gtk.Template.Child()

    lightbox_revealer = Gtk.Template.Child()
    lightbox_picture = Gtk.Template.Child()
    lightbox_video = Gtk.Template.Child()
    lightbox_caption = Gtk.Template.Child()
    lightbox_prev_btn = Gtk.Template.Child()
    lightbox_next_btn = Gtk.Template.Child()
    lightbox_close_btn = Gtk.Template.Child()
    lightbox_fav_btn = Gtk.Template.Child()

    edit_revealer = Gtk.Template.Child()
    edit_image_slot = Gtk.Template.Child()
    edit_brightness = Gtk.Template.Child()
    edit_contrast = Gtk.Template.Child()
    edit_saturation = Gtk.Template.Child()
    edit_exposure = Gtk.Template.Child()
    edit_temperature = Gtk.Template.Child()
    edit_cancel_btn = Gtk.Template.Child()
    edit_save_btn = Gtk.Template.Child()
    edit_rotate_left_btn = Gtk.Template.Child()
    edit_rotate_right_btn = Gtk.Template.Child()
    edit_crop_btn = Gtk.Template.Child()
    edit_flip_h_btn = Gtk.Template.Child()
    edit_flip_v_btn = Gtk.Template.Child()
    edit_filter_flow = Gtk.Template.Child()
    edit_tools = Gtk.Template.Child()
    edit_crop_panel = Gtk.Template.Child()

    PANEL_WIDTH = 300

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.con = lib.connect()
        self.settings = Gio.Settings.new(APP_ID)

        self.view = "all_photos"
        self._last_tab = "all_photos"
        self._show_hidden = False  # "Show hidden photos" toggle (primary menu)
        # What the detail page is showing: ("album", id) or ("period", kind, key, title).
        self._detail_source = None
        self._search_query = ""
        self._photos_all = []
        self._albums_all = []
        self._user_albums = []
        self._folder_nodes = {}    # path -> tree node (see lib.folder_tree)
        self._folder_roots = []    # top-level folder paths
        self._persons_all = []
        self._photo_people = {}   # photo_id -> [names], for search
        self._map_view = None
        self._gps_backfilled = False
        self._visible_photos = []
        self._visible_favs = []
        self._detail_photos = []
        self._surface_width = 0
        self._surface_height = 0
        self._photo_cell = 0    # square tile size, set from real grid width
        self._card_cell = 0     # square cover size for month/year/album cards
        self._thumb_size = self.settings.get_int("thumb-size")
        self._info_photo_id = None
        self._info_preview = None
        self._selected_tile = None

        self._lightbox_photos = []
        self._lightbox_index = 0

        self._edit_image = None
        self._edit_texture = None
        self._edit_photo = None
        self._edit_photo_id = None
        self._crop_backup = None
        self._applied_crop = None

        self._sort = {group: self.settings.get_string(f"sort-{group}")
                      for group in SORT_OPTIONS}
        # Photos used to offer a "name" sort; fall back to newest if that's what
        # was saved, so the (now Newest/Oldest) menu has a valid selection.
        if self._sort["photos"] not in ("date", "date-asc"):
            self._sort["photos"] = "date"

        self._tab_buttons = {
            "all_photos": self.tab_all_photos,
            "months": self.tab_months,
            "years": self.tab_years,
            "albums": self.tab_albums,
            "favourites": self.tab_favourites,
            "map": self.tab_map,
            "people": self.tab_people,
        }

        self._setup_actions()
        self._setup_window_controls()
        self._setup_lists()
        self._setup_map()
        self._setup_info_panel()
        self._setup_lightbox()
        self._setup_editor()
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
        self._setup_dnd()
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
        # Photo grids rebind from their stores (bind reads the current thumb
        # size). Month/Year period cards are a fixed size, so they don't change;
        # only the drill-down detail grid needs re-rendering.
        self._size_all_grids()
        self._apply_filters()
        if self.view == "detail":
            self._render_detail()

    # The thumbnail slider chooses how many photos sit across the grid rather
    # than a fixed pixel size: tiles fill their column, so at the largest end
    # one photo is ~a third of the paper, and at the smallest end many fit.
    _MIN_COLS, _MAX_COLS = 3, 12

    def _columns_for_thumb(self):
        lo, hi = 110, 320  # matches the slider / thumb-size range
        t = max(0.0, min(1.0, (self._thumb_size - lo) / float(hi - lo)))
        # Bigger slider -> fewer, larger columns; max slider -> 3 (≈33% each).
        return round(self._MAX_COLS - t * (self._MAX_COLS - self._MIN_COLS))

    # ---- grid layout, defined once and driven by each grid's real width ----
    #
    # Every view lays photos/cards on the same grid; only the content differs.
    # GtkGridView's own column heuristic (min/max columns + child natural size)
    # proved unreliable for "square cells that fill the column" — it left tall
    # rectangles or row gaps in some views but not others, because it doesn't
    # dependably re-measure a cell's height for the width it finally allocates.
    #
    # So we don't rely on it. We read each grid's *actual* allocated width, pick
    # an exact column count, force min-columns == max-columns to that count, and
    # size every swatch to the resulting column width. Cells are then square by
    # construction, identically in every view. Because fill-mode swatches may
    # shrink to width 0 (see Swatch.set_fill) and the scrollers never scroll
    # horizontally, forcing min == max can't push the grid wider — no layout
    # loop. Sizing re-runs whenever a grid's width changes (its scroller's
    # hadjustment "changed" fires on allocation) and when the slider or info
    # panel changes the geometry.
    PHOTO_GRIDS = ("photo_grid", "fav_grid", "detail_photos_grid")
    CARD_GRIDS = ("album_grid", "months_grid", "years_grid", "people_grid")
    GRID_MARGIN = SPACE_L   # 24px page margin around every grid (set in .ui)
    THUMB_GAP = 1           # px gap between adjacent photo thumbnails
    CARD_MARGIN = 8         # card box margin (each side) around its cover
    CARD_TARGET = 200       # ideal card width before adding another column
    CARD_MAX_COLS = 8

    def _photo_grids(self):
        return (self.photo_grid, self.fav_grid, self.detail_photos_grid)

    def _card_grids(self):
        return (self.album_grid, self.detail_folders_grid,
                self.months_grid, self.years_grid, self.people_grid)

    def _cell_px(self):
        """Best current square size for a photo tile — the last value computed
        from a real grid width, or an estimate before any grid is realised.
        Used by _make_tile / _bind_tile so freshly recycled tiles start at the
        right size (then _size_grid keeps them exact)."""
        if self._photo_cell:
            return self._photo_cell
        return self._estimate_cell(self._columns_for_thumb(), self.THUMB_GAP)

    def _card_cell_px(self):
        if self._card_cell:
            return self._card_cell
        return self._estimate_cell(4, 2 * self.CARD_MARGIN)

    def _estimate_cell(self, n, gap):
        # Pre-realisation fallback: derive the paper width from the surface the
        # same way _apply_layout_metrics does, so the first paint is close.
        w = self._surface_width or 1180
        margin_x = max(SPACE_L, round(w * 0.05))
        paper = w - 2 * margin_x
        if self.info_revealer.get_reveal_child():
            paper -= round(w * 0.04) + self.PANEL_WIDTH
        content = max(240, paper - 2 * self.GRID_MARGIN)
        return max(120, (content // max(1, n)) - gap)

    @staticmethod
    def _enclosing_scroller(widget):
        # The grid may sit a couple of boxes down from its GtkScrolledWindow
        # (the detail grid lives inside detail_box), so walk up to find it.
        node = widget.get_parent()
        while node is not None and not isinstance(node, Gtk.ScrolledWindow):
            node = node.get_parent()
        return node

    def _setup_grid_sizing(self):
        # Re-size a grid whenever it is allocated a new width. Its enclosing
        # GtkScrolledWindow's hadjustment emits "changed" on every allocation,
        # which is our width-change hook.
        for grid in self._photo_grids() + self._card_grids():
            scroller = self._enclosing_scroller(grid)
            adj = scroller.get_hadjustment() if scroller is not None else None
            if adj is not None:
                adj.connect("changed", lambda _a, g=grid: self._size_grid(g))

    def _size_grid(self, grid):
        """Force exact columns and square swatches for one grid from its real
        width. A no-op until the grid has been allocated (width > 1); the
        hadjustment "changed" signal re-invokes us once it has."""
        w = grid.get_width()
        if w <= 1:
            return
        if grid in self._photo_grids():
            n = max(1, min(self._MAX_COLS, self._columns_for_thumb()))
            # A tile's swatch fills the column minus its trailing gap, so the
            # height must equal exactly that width to stay square.
            cell = max(1, (w // n) - self.THUMB_GAP)
            self._photo_cell = cell
        else:
            n = max(2, min(self.CARD_MAX_COLS, w // self.CARD_TARGET))
            # A card's cover fills the column minus the card's side margins.
            cell = max(1, (w // n) - 2 * self.CARD_MARGIN)
            self._card_cell = cell
        # Only touch the column count when it actually changes: set_min/max
        # queues a resize, so re-setting the same value on every "changed" would
        # spin. With the count stable and swatch sizes converged, nothing else
        # queues a resize and the layout settles.
        if grid.get_min_columns() != n or grid.get_max_columns() != n:
            grid.set_min_columns(n)
            grid.set_max_columns(n)
        self._resize_swatches(grid, cell)

    @staticmethod
    def _resize_swatches(root, cell):
        stack = [root]
        while stack:
            widget = stack.pop()
            swatch = getattr(widget, "swatch", None)
            if isinstance(swatch, Swatch):
                swatch.set_size(cell)  # no-op if unchanged
            child = widget.get_first_child()
            while child:
                stack.append(child)
                child = child.get_next_sibling()

    def _size_all_grids(self):
        for grid in self._photo_grids() + self._card_grids():
            self._size_grid(grid)

    def _schedule_resize_tiles(self):
        GLib.idle_add(self._size_all_grids)

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
        self._schedule_resize_tiles()
        return False

    def _apply_layout_metrics(self):
        """5% margins. The outer page margins stay put whether or not the info
        panel is open — the panel slides in on the right (via the revealer) and
        the paper reflows into the remaining width. Keeping margin_x fixed is
        what makes the transition smooth: re-centering the paper on reveal made
        the whole grid jump sideways."""
        width, height = self._surface_width, self._surface_height
        if width <= 0 or height <= 0:
            return
        margin_y = round(height * 0.05)
        margin_x = max(SPACE_L, round(width * 0.05))
        revealed = self.info_revealer.get_reveal_child()
        gap = round(width * 0.04) if revealed else 0
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

        use_edited = Gio.SimpleAction.new("use-edited", GLib.VariantType.new("s"))
        use_edited.connect("activate", self._on_use_edited)
        self.add_action(use_edited)

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

        show_hidden = Gio.SimpleAction.new_stateful(
            "show-hidden", None, GLib.Variant("b", self._show_hidden))
        show_hidden.connect("activate", self._on_show_hidden)
        self.add_action(show_hidden)

        item_actions = Gio.SimpleActionGroup()
        for name in ("open", "edit-image", "edit-info", "add-to-album",
                     "add-to-new-album", "set-cover", "toggle-fav",
                     "hide", "trash",
                     "rename-album", "rename-person", "delete"):
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
            "activate", lambda g, p: self._activate_album_item(g.get_model().get_item(p)))

        # Sub-folders shown at the top of a folder's detail page (the tree view).
        self.detail_folders_store = Gio.ListStore(item_type=Album)
        self.detail_folders_grid.set_model(
            Gtk.SingleSelection(model=self.detail_folders_store))
        self.detail_folders_grid.set_factory(self._factory(self._bind_album_card))
        self.detail_folders_grid.set_single_click_activate(True)
        self.detail_folders_grid.connect(
            "activate", lambda g, p: self._activate_album_item(g.get_model().get_item(p)))

        self.fav_store = Gio.ListStore(item_type=Photo)
        self.fav_grid.set_model(Gtk.NoSelection(model=self.fav_store))
        self.fav_grid.set_factory(self._factory(lambda it: self._bind_tile_item(it, "favourites")))

        self.detail_store = Gio.ListStore(item_type=Photo)
        self.detail_photos_grid.set_model(Gtk.NoSelection(model=self.detail_store))
        self.detail_photos_grid.set_factory(self._factory(lambda it: self._bind_tile_item(it, "detail")))

        # Months / Years show bounded period cards, not a tile per photo.
        self.months_store = Gio.ListStore(item_type=Period)
        self.months_grid.set_model(Gtk.SingleSelection(model=self.months_store))
        self.months_grid.set_factory(self._factory(self._bind_period_card))
        self.months_grid.set_single_click_activate(True)
        self.months_grid.connect("activate", self._on_period_activated)

        self.years_store = Gio.ListStore(item_type=Period)
        self.years_grid.set_model(Gtk.SingleSelection(model=self.years_store))
        self.years_grid.set_factory(self._factory(self._bind_period_card))
        self.years_grid.set_single_click_activate(True)
        self.years_grid.connect("activate", self._on_period_activated)

        self.people_store = Gio.ListStore(item_type=Person)
        self.people_grid.set_model(Gtk.SingleSelection(model=self.people_store))
        self.people_grid.set_factory(self._factory(self._bind_person_card))
        self.people_grid.set_single_click_activate(True)
        self.people_grid.connect(
            "activate", lambda g, p: self._open_person(g.get_model().get_item(p).id))

        self._setup_grid_sizing()

    def _setup_map(self):
        # Easel's map is the built-in offline vector map: no tiles, no network,
        # no image decoding (all of which are unreliable in the sandbox). It's
        # the only map, so there's nothing to configure here.
        self._map_view = MapView()
        self._map_view.set_activate_cb(self._on_map_pin)
        self.map_slot.append(self._map_view)

    def _on_period_activated(self, gridview, position):
        period = gridview.get_model().get_item(position)
        if period is not None:
            self._open_period(period.kind, period.key, period.title)

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
        # 1px margins → a 2px gap between neighbouring tiles.
        # A 1px trailing margin on the right/bottom is the only gap between
        # thumbnails, so neighbours sit 1px apart (the grid's own margin bounds
        # the outer edges).
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                      margin_end=self.THUMB_GAP, margin_bottom=self.THUMB_GAP)
        box.set_cursor(POINTER_CURSOR)
        box.add_css_class("card-box")

        overlay = Gtk.Overlay()
        # Fills its grid cell (width from the column, height pinned to the same
        # size) so tiles are squares that every row shares — sharp-cornered,
        # cover-cropped 1:1.
        swatch = Swatch("", size=self._cell_px())
        swatch.add_css_class("photo-tile")
        swatch.set_fill(True)
        overlay.set_child(swatch)

        # Single favourite control at the bottom-right: shown on hover or when
        # favourited; clicking it colours in place (it never moves).
        fav = Gtk.Button(icon_name="easel-heart-symbolic", halign=Gtk.Align.END,
                         valign=Gtk.Align.END, margin_bottom=12, margin_end=12,
                         tooltip_text="Favourite", css_classes=["tile-fav"])
        fav.set_visible(False)
        fav.set_cursor(POINTER_CURSOR)
        fav.connect("clicked", lambda _b: self._toggle_fav(box._photo.id))
        overlay.add_overlay(fav)

        # Centered play badge marks videos (which don't get a decoded thumbnail).
        play = Gtk.Image(icon_name="media-playback-start-symbolic",
                         halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                         css_classes=["video-badge"])
        play.set_visible(False)
        overlay.add_overlay(play)

        # Top-right control opens the photo full screen — clicking a photo
        # already surfaces its actions in the info panel, so a quick "view big"
        # button is more useful here than the old ⋯ menu (right-click still has
        # the full menu).
        fs_btn = Gtk.Button(icon_name="easel-maximize-symbolic", halign=Gtk.Align.END,
                            valign=Gtk.Align.START, margin_top=12, margin_end=12,
                            tooltip_text="View full screen", css_classes=["card-menu-btn"])
        fs_btn.set_visible(False)
        fs_btn.set_cursor(POINTER_CURSOR)
        fs_btn.connect("clicked", lambda _b: self._open_photo_by_id(box._photo.id)
                       if box._photo else None)
        overlay.add_overlay(fs_btn)

        box.append(overlay)
        box.swatch, box.fav, box.fs_btn = swatch, fav, fs_btn
        box.play = play
        box._photo = None
        box._source = "photos"

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_a: (box.fav.set_visible(True),
                                             box.fs_btn.set_visible(True)))
        motion.connect("leave", lambda *_a: self._tile_unhover(box))
        box.add_controller(motion)
        box._motion = motion

        left = Gtk.GestureClick(button=1)
        left.connect("pressed", lambda _g, n, x, y: self._tile_pressed(n, box, x, y))
        box.add_controller(left)

        right = Gtk.GestureClick(button=3)
        right.connect("pressed", lambda _g, _n, x, y: self._show_item_menu(box, box, x, y))
        box.add_controller(right)

        box.set_has_tooltip(True)
        box.connect("query-tooltip", self._on_tile_tooltip)
        return box

    @staticmethod
    def _heart_icon(fav):
        # Filled (gold) heart once favourited; outline heart otherwise.
        return "easel-heart-filled-symbolic" if fav else "easel-heart-symbolic"

    @staticmethod
    def _tile_faved(box):
        return box._photo is not None and box._photo.favorite

    def _tile_unhover(self, box):
        # The heart stays visible for favourited photos; the fullscreen button
        # only shows on hover.
        box.fav.set_visible(self._tile_faved(box))
        box.fs_btn.set_visible(False)

    def _bind_tile(self, tile, photo, source_name):
        tile._photo = photo
        tile._source = source_name
        tile._menu_kind = "photo"
        tile._menu_item_id = photo.id
        tile._menu_entries = PHOTO_ENTRIES
        tile._menu_extra = {}
        tile.swatch.set_size(self._cell_px())
        tile.swatch.set_placeholder("video" if photo.is_video else "")
        tile.swatch.set_path(photo.path or None, rotation=photo.rotation)
        tile.play.set_visible(photo.is_video)
        # Favourite heart: filled + gold when faved (always shown); outline on
        # hover otherwise.
        tile.fav.set_visible(photo.favorite or tile._motion.get_property("contains-pointer"))
        tile.fav.set_icon_name(self._heart_icon(photo.favorite))
        if photo.favorite:
            tile.fav.add_css_class("faved")
        else:
            tile.fav.remove_css_class("faved")
        # Highlight the tile whose photo is open in the info panel.
        if photo.id == self._info_photo_id:
            tile.add_css_class("tile-selected")
            self._selected_tile = tile
        else:
            tile.remove_css_class("tile-selected")

    def _tile_pressed(self, n_press, tile, x=None, y=None):
        photo = tile._photo
        if photo is None:
            return
        # A press that lands on an overlay control (the heart or fullscreen
        # button) belongs to that button alone — it must not also select the
        # photo or open the info panel.
        if x is not None:
            picked = tile.pick(x, y, Gtk.PickFlags.DEFAULT)
            while picked is not None and picked is not tile:
                if picked is tile.fav or picked is tile.fs_btn:
                    return
                picked = picked.get_parent()
        if n_press >= 2:
            source = self._source_for(tile._source)
            ids = [p.id for p in source]
            index = ids.index(photo.id) if photo.id in ids else 0
            self._open_lightbox(source if source else [photo], index)
        elif n_press == 1:
            # Open the info panel immediately (no double-click debounce, which
            # made the panel feel laggy). Clicking a photo — even the open one —
            # keeps the panel open and re-selects it; closing is the close
            # button's job. A double-click still opens the lightbox on top.
            self._select_tile(tile)
            if not (self._info_photo_id == photo.id
                    and self.info_revealer.get_reveal_child()):
                self._show_info(photo.id)

    def _select_tile(self, tile):
        """Move the blue selection ring to `tile` (or clear it with None)."""
        if self._selected_tile is tile:
            return
        if self._selected_tile is not None:
            try:
                self._selected_tile.remove_css_class("tile-selected")
            except Exception:
                pass
        self._selected_tile = tile
        if tile is not None:
            tile.add_css_class("tile-selected")

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
        box.swatch.set_size(self._card_cell_px())
        box.swatch.set_placeholder("album")
        box.swatch.set_path(album.cover_path or None)
        box.title.set_label(album.title)
        count = album.photo_count
        parts = [f"{count} photo{'s' if count != 1 else ''}"]
        if album.folder and album.subfolder_count:
            n = album.subfolder_count
            parts.append(f"{n} folder{'s' if n != 1 else ''}")
        box.subtitle.set_label(" · ".join(parts))
        if album.folder:
            # A directory node in the tree: no album rename/delete menu.
            box._no_menu = True
            box.menu_btn.set_visible(False)
        else:
            box._no_menu = False
            self._attach_album_menu(box, album.id)

    def _album_card_widget(self):
        # No fixed width: the card fills its grid column (see _size_grid) and the
        # cover stays a 1:1 square at whatever width the column gives it.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        box.set_cursor(POINTER_CURSOR)
        box.add_css_class("card-box")
        swatch = Swatch("", size=self._card_cell_px())
        swatch.add_css_class("card-cover")
        swatch.set_fill(True)  # fill the card width and stay square (1:1)

        text_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["card-title"])
        subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["mono-dim-sm"])
        self._tooltip_when_ellipsized(title)
        self._tooltip_when_ellipsized(subtitle)
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
        box._no_menu = False   # folder-node cards set this to suppress the menu

        motion = Gtk.EventControllerMotion()
        motion.connect("enter",
                       lambda *_a: box.menu_btn.set_visible(not box._no_menu))
        motion.connect("leave",
                       lambda *_a: None if box._menu_open else box.menu_btn.set_visible(False))
        box.add_controller(motion)
        box._motion = motion

        def on_menu_clicked(btn):
            box._menu_open = True
            popover = self._show_item_menu(box, btn, btn.get_width() / 2, btn.get_height())

            def on_closed(_p):
                box._menu_open = False
                if not box._motion.get_property("contains-pointer"):
                    box.menu_btn.set_visible(False)

            popover.connect("closed", on_closed)

        menu_btn.connect("clicked", on_menu_clicked)
        # Right-click opens the same menu; folder-node cards (_no_menu) skip it.
        gesture = Gtk.GestureClick(button=3)
        gesture.connect("pressed", lambda _g, _n, x, y:
                        None if box._no_menu else self._show_item_menu(box, box, x, y))
        box.add_controller(gesture)
        return box

    def _attach_album_menu(self, box, album_id):
        box._menu_kind = "album"
        box._menu_item_id = album_id
        box._menu_entries = ALBUM_ENTRIES
        box._menu_extra = {}

    # ---------- period views (Months / Years) ----------

    # (kind -> (key strftime, title strftime)). "Undated" catches bad/zero dates.
    _PERIOD_FMT = {"month": ("%Y-%m", "%B %Y"), "year": ("%Y", "%Y")}

    @staticmethod
    def _period_of(date_taken, kind):
        key_fmt, title_fmt = EaselWindow._PERIOD_FMT[kind]
        try:
            dt = datetime.fromtimestamp(date_taken)
            return dt.strftime(key_fmt), dt.strftime(title_fmt)
        except (ValueError, OSError):
            return "undated", "Undated"

    def _compute_periods(self, kind):
        """Bucket the visible photos into month/year Periods, newest first, each
        with its newest photo as the cover. Bounded to a handful (years) or a
        few hundred (months) cards — never one per photo."""
        order = []
        buckets = {}
        for p in sorted(self._visible_photos, key=lambda p: -p.date_taken):
            key, title = self._period_of(p.date_taken, kind)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = buckets[key] = {"title": title, "count": 0, "cover": p.path}
                order.append(key)
            bucket["count"] += 1
        periods = []
        for key in order:
            b = buckets[key]
            n = b["count"]
            periods.append(Period(kind=kind, key=key, title=b["title"],
                                  subtitle=f"{n} photo{'s' if n != 1 else ''}",
                                  cover_path=b["cover"] or ""))
        return periods

    def _render_months(self):
        self._fill_period_store(self.months_store, self._compute_periods("month"))

    def _render_years(self):
        self._fill_period_store(self.years_store, self._compute_periods("year"))

    def _fill_period_store(self, store, periods):
        self._fill_store(store, periods)

    def _bind_period_card(self, item):
        period = item.get_item()
        box = item.get_child()
        if not hasattr(box, "swatch"):
            box = self._period_card_widget()
            item.set_child(box)
        box.swatch.set_size(self._card_cell_px())
        box.swatch.set_placeholder(period.title)
        box.swatch.set_path(period.cover_path or None)
        box.title.set_label(period.title)
        box.subtitle.set_label(period.subtitle)

    def _period_card_widget(self):
        # No fixed width: the card fills its grid column (see _size_grid) and the
        # cover stays a 1:1 square at whatever width the column gives it.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        box.set_cursor(POINTER_CURSOR)
        box.add_css_class("card-box")
        swatch = Swatch("", size=self._card_cell_px())
        swatch.add_css_class("card-cover")
        swatch.set_fill(True)  # fill the card width and stay square (1:1)
        title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["card-title"])
        subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["mono-dim-sm"])
        box.append(swatch)
        box.append(title)
        box.append(subtitle)
        box.swatch, box.title, box.subtitle = swatch, title, subtitle
        return box

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
                for album in self._user_albums:
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
            if action == "hide":
                label = "Unhide" if lib.is_hidden(self.con, widget._menu_item_id) else "Hide"
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
        if name == "hide":
            self._toggle_hidden(item_id)
            return
        if name == "trash":
            self._confirm_trash(item_id)
            return
        if name == "open" and kind == "album":
            self._select_tab("albums")
            self._open_album(item_id)
            return
        if name == "open" and kind == "person":
            self._select_tab("people")
            self._open_person(item_id)
            return
        if name == "rename-person":
            person = lib.get_person(self.con, item_id)
            if person:
                self._prompt_name(
                    "Rename Person", person["name"],
                    lambda text: self._do_rename_person(item_id, text))
            return
        if name == "edit-image":
            self._open_editor(item_id)
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
            if lib.in_album(self.con, data["album"], item_id):
                self._toast("This photo is already in the album")
                return
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

    def _do_rename_person(self, person_id, text):
        if not lib.rename_person(self.con, person_id, text):
            self._toast("Another person already has that name")
            return
        self._reload_all()

    def _toggle_fav(self, photo_id):
        row = lib.get_photo(self.con, photo_id)
        if not row:
            return
        lib.set_favorite(self.con, photo_id, not row["favorite"])
        self._reload_all()

    def _toggle_hidden(self, photo_id):
        """Hide a photo (reversible, file untouched) or un-hide it. Hiding puts
        up an Undo toast so it's a safe, one-click-back action."""
        now_hidden = not lib.is_hidden(self.con, photo_id)
        lib.set_photo_hidden(self.con, photo_id, now_hidden)
        if self._info_photo_id == photo_id and now_hidden and not self._show_hidden:
            self._close_info()
        self._reload_all()
        if now_hidden:
            toast = Adw.Toast.new("Photo hidden")
            toast.set_button_label("Undo")
            toast.connect("button-clicked",
                          lambda _t: (lib.set_photo_hidden(self.con, photo_id, False),
                                      self._reload_all()))
            self.toast_overlay.add_toast(toast)
        else:
            self._toast("Photo unhidden")

    def _confirm_trash(self, photo_id):
        """Move the file to the system Trash — the only action in Easel that
        touches the file on disk, so it's clearly fenced behind a warning. The
        Trash is recoverable from the file manager; we never permanently
        delete."""
        row = lib.get_photo(self.con, photo_id)
        if not row:
            return
        name = os.path.basename(row["path"])
        dialog = Adw.AlertDialog(
            heading="Move to Trash?",
            body=(f"“{name}” will be moved to your system Trash. This removes it "
                  "from disk — you can restore it from Trash in your file manager. "
                  "To simply tuck it out of Easel without touching the file, use "
                  "Hide instead."))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("trash", "Move to Trash")
        dialog.set_response_appearance("trash", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect("response",
                       lambda _d, r: self._do_trash(photo_id) if r == "trash" else None)
        dialog.present(self)

    def _do_trash(self, photo_id):
        row = lib.get_photo(self.con, photo_id)
        if not row:
            return
        ok, err = self._trash_file(row["path"])
        if not ok:
            self._toast(f"Couldn’t move to Trash: {err}")
            return
        if self._info_photo_id == photo_id:
            self._close_info()
        lib.delete_photo(self.con, photo_id)
        self._reload_all()
        self._toast("Moved to Trash")

    def _trash_file(self, path):
        """Move a file to the system Trash, returning (ok, error_message).

        Easel only has read-only access to the photo folders (see the Flatpak
        manifest), so it can't move the file itself. The Trash portal does it
        for us: it runs outside the sandbox with the user's own permissions, so
        a read-only fd is enough to identify the file it should trash. We fall
        back to Gio's direct trash for when Easel runs unsandboxed (developer
        builds), where the portal may be absent."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError as err:
            return False, err.strerror
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            fd_list = Gio.UnixFDList.new()
            handle = fd_list.append(fd)  # dups the fd; we still own ours
            reply, _out = bus.call_with_unix_fd_list_sync(
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.Trash",
                "TrashFile",
                GLib.Variant("(h)", (handle,)),
                GLib.VariantType.new("(u)"),
                Gio.DBusCallFlags.NONE, -1, fd_list, None)
            # The portal returns 1 on success, 0 on failure.
            if reply.unpack()[0] == 1:
                return True, None
            portal_err = "the Trash portal declined"
        except GLib.Error as err:
            portal_err = err.message
        finally:
            os.close(fd)
        # No portal (unsandboxed dev run): try Gio's own trash.
        try:
            Gio.File.new_for_path(path).trash(None)
            return True, None
        except GLib.Error as err:
            return False, f"{portal_err}; {err.message}"

    def _confirm_delete(self, kind, item_id):
        if kind == "album":
            album = lib.get_album(self.con, item_id)
            if album and album["user_created"]:
                heading, body = "Delete album?", "This deletes the album. The photos stay in your library."
            else:
                heading = "Remove album?"
                body = ("This removes the folder's photos from your library. "
                        "Files on disk are not touched.")
        elif kind == "person":
            person = lib.get_person(self.con, item_id)
            heading = "Remove person?"
            body = (f"This removes {person['name']} and all their tags. "
                    "Your photos are not touched.") if person else "Remove this person?"
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
        {"photo": lib.delete_photo, "album": lib.delete_album,
         "person": lib.delete_person}[kind](self.con, item_id)
        if kind == "photo" and self._info_photo_id == item_id:
            self._close_info()
        if (self.view == "detail" and self._detail_source
                and self._detail_source[0] == kind
                and self._detail_source[1] == item_id):
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
        # Square preview sized to the panel width — a fixed size, so the panel
        # can't be stretched wide by a big image's natural size (the old bug).
        self._info_preview = Swatch("", size=self.PANEL_WIDTH)
        self._info_preview.add_css_class("info-preview")
        # A pin overlay on top of the preview shows tagged people and turns a
        # click on the photo into a place-a-name action.
        self._face_layer = FacePinLayer()
        self._face_layer.set_place_cb(self._on_face_place)
        preview_overlay = Gtk.Overlay()
        preview_overlay.set_child(self._info_preview)
        preview_overlay.add_overlay(self._face_layer)
        self.info_preview_slot.set_child(preview_overlay)
        self.info_close_btn.connect("clicked", lambda *_: self._close_info())
        self.info_fullscreen_btn.connect("clicked", lambda *_: self._info_fullscreen())
        self.info_rotate_left_btn.connect("clicked", lambda *_: self._rotate_info(-90))
        self.info_rotate_right_btn.connect("clicked", lambda *_: self._rotate_info(90))
        self.info_add_btn.connect("clicked", self._on_info_add)
        self.info_edit_btn.connect(
            "clicked", lambda *_: self._open_editor(self._info_photo_id)
            if self._info_photo_id else None)

    def _photo_rotation(self, row):
        return (row["rotation"] or 0) if "rotation" in row.keys() else 0

    def _show_info(self, photo_id):
        row = lib.get_photo(self.con, photo_id)
        if not row:
            return
        self._info_photo_id = photo_id
        self._info_preview.set_size(self.PANEL_WIDTH)
        self._info_preview.set_path(row["path"], rotation=self._photo_rotation(row))

        self._clear_box(self.info_rows_box)
        # People tagged in this photo, with their pins shown on the preview.
        faces = lib.faces_for_photo(self.con, photo_id)
        self._face_layer.set_faces([(f["name"], f["x"], f["y"]) for f in faces])
        self.info_rows_box.append(self._people_section(photo_id, faces))
        self.info_rows_box.append(self._info_divider())
        dims = _dimensions(row["path"])
        try:
            size = _fmt_size(os.path.getsize(row["path"]))
        except OSError:
            size = "—"
        path = row["path"]
        # Order (Figma): [In this photo — once people exist] · Album · divider ·
        # Date · Dimensions · Size · File name · Path. Clicking Path opens the
        # file in the system file manager.
        self.info_rows_box.append(self._info_row("Album", row["album_title"] or "—"))
        self.info_rows_box.append(self._info_divider())
        self.info_rows_box.append(self._info_row("Date", _fmt_date(row["date_taken"]) or "Undated"))
        self.info_rows_box.append(self._info_row("Dimensions", f"{dims[0]} × {dims[1]}" if dims else "—"))
        self.info_rows_box.append(self._info_row("Size", size))
        self.info_rows_box.append(self._info_row("File name", os.path.basename(path)))
        self.info_rows_box.append(
            self._info_row("Path", path, on_click=lambda p=path: self._open_in_files(p)))

        # Opening the panel narrows the grid, which reflows the columns and would
        # otherwise make the photo you just clicked jump. Record where it sits
        # now, then re-anchor it there once the grid has reflowed.
        self._anchor_selection(photo_id)

        self.info_revealer.set_visible(True)
        self.info_revealer.set_reveal_child(True)
        self._apply_layout_metrics()
        self._schedule_resize_tiles()

    def _grid_and_source(self):
        return {
            "all_photos": (self.photo_grid, self._visible_photos),
            "favourites": (self.fav_grid, self._visible_favs),
            "detail": (self.detail_photos_grid, self._detail_photos),
        }.get(self.view, (None, None))

    def _find_tile(self, grid, photo_id):
        """The realised tile widget bound to photo_id, or None. Walks only the
        grid's live (virtualised) subtree, so it's cheap."""
        stack = [grid]
        while stack:
            w = stack.pop()
            p = getattr(w, "_photo", None)
            if p is not None and p.id == photo_id:
                return w
            child = w.get_first_child()
            while child:
                stack.append(child)
                child = child.get_next_sibling()
        return None

    def _grid_geometry(self, grid, source):
        """Infer the grid's layout from its realised tiles: (ncols, row_height,
        y0) such that model index j sits at content-y = y0 + (j // ncols) *
        row_height. Uses tile positions (content coordinates), so it needs no
        knowledge of CSS margins/spacing. None if too few tiles are realised."""
        index_of = {p.id: k for k, p in enumerate(source)}
        samples = []  # (index, content_y)
        stack = [grid]
        while stack:
            w = stack.pop()
            p = getattr(w, "_photo", None)
            if p is not None and p.id in index_of:
                ok, pt = w.compute_point(grid, Graphene.Point().init(0, 0))
                if ok:
                    samples.append((index_of[p.id], round(pt.y)))
            child = w.get_first_child()
            while child:
                stack.append(child)
                child = child.get_next_sibling()
        if len(samples) < 2:
            return None
        rows = sorted({y for _, y in samples})
        gaps = [b - a for a, b in zip(rows, rows[1:]) if b - a > 1]
        if not gaps:
            return None  # only one row realised — can't tell the row pitch
        row_h = min(gaps)
        counts = {}
        for _, y in samples:
            counts[y] = counts.get(y, 0) + 1
        ncols = max(counts.values())  # a full row reveals the column count
        if ncols < 1:
            return None
        j, yj = samples[0]
        y0 = yj - (j // ncols) * row_h
        return ncols, row_h, y0

    def _anchor_selection(self, photo_id):
        """Remember the clicked photo's viewport offset, then re-anchor it to
        that offset after the grid reflows. Triggered on the scroller's
        'changed' signal (fires once the reflow updates the content height), so
        the measurement is taken after the new column count is in effect."""
        grid, _ = self._grid_and_source()
        if grid is None:
            return
        scrolled = grid.get_ancestor(Gtk.ScrolledWindow)
        tile = self._find_tile(grid, photo_id)
        if tile is None or scrolled is None:
            return
        ok, pt = tile.compute_point(grid, Graphene.Point().init(0, 0))
        if not ok:
            return
        vadj = scrolled.get_vadjustment()
        offset = pt.y - vadj.get_value()  # where in the viewport it sits now

        state = {"done": False, "handler": 0, "timeout": 0}

        def finish():
            if state["done"]:
                return
            state["done"] = True
            if state["handler"]:
                vadj.disconnect(state["handler"])
            if state["timeout"]:
                GLib.source_remove(state["timeout"])

        def on_changed(_adj):
            finish()
            self._reanchor_photo(photo_id, offset)

        state["handler"] = vadj.connect("changed", on_changed)
        # If the column count didn't actually change, 'changed' won't fire and
        # nothing needs re-anchoring — just drop the handler after a moment.
        state["timeout"] = GLib.timeout_add(200, lambda: (finish(), False)[1])

    def _reanchor_photo(self, photo_id, offset):
        grid, source = self._grid_and_source()
        if grid is None:
            return
        scrolled = grid.get_ancestor(Gtk.ScrolledWindow)
        if scrolled is None:
            return
        geom = self._grid_geometry(grid, source)
        try:
            i = next(k for k, p in enumerate(source) if p.id == photo_id)
        except StopIteration:
            return
        vadj = scrolled.get_vadjustment()
        if geom is None:
            grid.scroll_to(i, Gtk.ListScrollFlags.NONE, None)  # at least keep it visible
            return
        ncols, row_h, y0 = geom
        content_y = y0 + (i // ncols) * row_h
        target = content_y - offset
        target = max(vadj.get_lower(),
                     min(target, vadj.get_upper() - vadj.get_page_size()))
        vadj.set_value(target)

    def _info_row(self, key, value, on_click=None):
        # Figma info rows: mono key on the left, value pushed to the right.
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        k = Gtk.Label(label=key, xalign=0, css_classes=["info-key"])
        v = Gtk.Label(label=value, xalign=1, hexpand=True, halign=Gtk.Align.END,
                      ellipsize=Pango.EllipsizeMode.END,
                      css_classes=["info-value"])
        self._tooltip_when_ellipsized(v)
        if on_click is not None:
            # e.g. the Path row: click opens the file in the file manager.
            v.add_css_class("info-path")
            v.set_cursor(POINTER_CURSOR)
            gesture = Gtk.GestureClick()
            gesture.connect("released", lambda *_a: on_click())
            v.add_controller(gesture)
        else:
            v.set_selectable(True)
        row.append(k)
        row.append(v)
        return row

    def _info_divider(self):
        return Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL,
                             css_classes=["info-divider"])

    def _open_in_files(self, path):
        """Open the system file manager with this file selected."""
        try:
            launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(path))
            launcher.open_containing_folder(self, None, None)
        except Exception:
            pass

    @staticmethod
    def _tooltip_when_ellipsized(label):
        """Show the label's full text as a tooltip, but only while it is
        actually truncated on screen — so untruncated text gets no redundant
        tooltip. Covers long filenames, people lists, album titles, etc."""
        label.set_has_tooltip(True)
        label.connect("query-tooltip", EaselWindow._on_label_query_tooltip)

    @staticmethod
    def _on_label_query_tooltip(label, x, y, keyboard, tooltip):
        layout = label.get_layout()
        if layout is not None and layout.is_ellipsized():
            tooltip.set_text(label.get_text())
            return True
        return False

    def _rotate_info(self, delta):
        if self._info_photo_id is None:
            return
        pid = self._info_photo_id
        row = lib.get_photo(self.con, pid)
        if not row:
            return
        new = (self._photo_rotation(row) + delta) % 360
        lib.set_rotation(self.con, pid, new)
        self._info_preview.set_path(row["path"], rotation=new)
        # Update just this photo's orientation in place — a full reload would
        # repopulate every grid and could disturb scroll/selection.
        for lst in (self._photos_all, self._visible_photos, self._visible_favs,
                    self._detail_photos):
            for p in lst:
                if p.id == pid:
                    p.rotation = new
        for store in (self.photo_store, self.fav_store, self.detail_store):
            for i in range(store.get_n_items()):
                if store.get_item(i).id == pid:
                    item = store.get_item(i)
                    store.remove(i)
                    store.insert(i, item)  # re-binds this tile with the new rotation
                    break

    def _on_info_add(self, button):
        if self._info_photo_id is None:
            return
        carrier = _MenuTarget("photo", self._info_photo_id,
                              [("Add to Favourites", "toggle-fav"), ("Add to", "__albums__")])
        popover = Gtk.PopoverMenu.new_from_model(self._build_item_menu(carrier))
        popover.set_has_arrow(True)
        popover.set_parent(button)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    # ---------- people tagging (info panel) ----------

    def _people_section(self, photo_id, faces):
        """The 'In this photo' info row: 'In this photo' key on the left, the
        tagged names as a comma-separated, right-aligned value. A name opens
        that person's photos on click; right-click offers to remove the tag.
        Clicking the photo above tags a new person at a pin."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Gtk.Label(label="In this photo", xalign=0,
                             valign=Gtk.Align.CENTER, css_classes=["info-key"]))
        # Names pushed to the right (hexpand claims the space; halign END places
        # them at the right edge like the other info values).
        names = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4,
                        hexpand=True, halign=Gtk.Align.END, valign=Gtk.Align.CENTER,
                        css_classes=["people-value"])
        if faces:
            last = len(faces) - 1
            for i, f in enumerate(faces):
                text = f["name"] + ("," if i < last else "")
                names.append(self._person_name_label(photo_id, f["person_id"], text))
        else:
            names.append(self._add_name_label(photo_id))
        row.append(names)
        return row

    def _person_name_label(self, photo_id, person_id, text):
        label = Gtk.Label(label=text, css_classes=["person-name"],
                          ellipsize=Pango.EllipsizeMode.END)
        label.set_cursor(POINTER_CURSOR)
        left = Gtk.GestureClick(button=1)
        left.connect("released", lambda *_a: self._open_person_nav(person_id))
        label.add_controller(left)
        right = Gtk.GestureClick(button=3)
        right.connect("pressed", lambda _g, _n, x, y:
                      self._person_remove_menu(label, photo_id, person_id, x, y))
        label.add_controller(right)
        return label

    def _add_name_label(self, photo_id):
        label = Gtk.Label(label="Add name", css_classes=["person-add"])
        label.set_cursor(POINTER_CURSOR)
        gesture = Gtk.GestureClick(button=1)
        gesture.connect(
            "released", lambda *_a: self._add_person_popover(photo_id, label, 0.5, 0.5))
        label.add_controller(gesture)
        return label

    def _open_person_nav(self, person_id):
        if lib.get_person(self.con, person_id):
            self._select_tab("people")
            self._open_person(person_id)

    def _person_remove_menu(self, anchor, photo_id, person_id, x, y):
        popover = Gtk.Popover(has_arrow=True, css_classes=["person-popover"])
        popover.set_parent(anchor)
        popover.set_pointing_to(Gdk.Rectangle(x=int(x), y=int(y), width=1, height=1))
        btn = Gtk.Button(label="Remove from photo", css_classes=["flat", "menu-item"])
        btn.set_cursor(POINTER_CURSOR)

        def do_remove(*_a):
            popover.popdown()
            self._untag_person(photo_id, person_id)

        btn.connect("clicked", do_remove)
        popover.set_child(btn)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    def _untag_person(self, photo_id, person_id):
        lib.remove_face(self.con, photo_id, person_id)
        self._people_changed(photo_id)

    def _on_face_place(self, nx, ny, px, py):
        if self._info_photo_id is not None:
            self._add_person_popover(self._info_photo_id, self._face_layer,
                                     nx, ny, px, py)

    def _add_person_popover(self, photo_id, anchor, x, y, px=None, py=None):
        popover = Gtk.Popover(css_classes=["person-popover"])
        popover.set_parent(anchor)
        if px is not None:
            popover.set_pointing_to(
                Gdk.Rectangle(x=int(px), y=int(py), width=1, height=1))
        entry = Gtk.Entry(placeholder_text="Name", activates_default=True)
        entry.set_size_request(180, -1)
        # Offer the names already in use so a person is re-tagged, not duplicated.
        store = Gtk.ListStore(str)
        for p in self._persons_all:
            store.append([p.name])
        completion = Gtk.EntryCompletion()
        completion.set_model(store)
        completion.set_text_column(0)
        completion.set_inline_completion(True)
        completion.set_popup_completion(True)
        entry.set_completion(completion)

        def commit(*_a):
            name = entry.get_text().strip()
            if name:
                lib.tag_person(self.con, photo_id, name, x, y)
                self._people_changed(photo_id)
            popover.popdown()

        entry.connect("activate", commit)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.set_child(entry)
        popover.popup()
        entry.grab_focus()

    def _people_changed(self, photo_id):
        """Refresh everything that reflects a tag change without a full reload:
        the People data, an open person page, and the info panel's section."""
        self._persons_all = [
            Person(id=r["id"], name=r["name"], photo_count=r["photo_count"] or 0,
                   cover_path=r["cover_path"] or "", date_taken=r["date_taken"] or 0.0)
            for r in lib.all_persons(self.con)
        ]
        self._photo_people = lib.people_by_photo(self.con)
        if self.view == "people":
            self._render_people()
        elif (self.view == "detail" and self._detail_source
              and self._detail_source[0] == "person"):
            self._render_detail()
        if self._info_photo_id == photo_id:
            self._show_info(photo_id)

    def _close_info(self):
        self.info_revealer.set_reveal_child(False)
        self.info_revealer.set_visible(False)
        self._info_photo_id = None
        self._select_tile(None)
        self._apply_layout_metrics()
        self._schedule_resize_tiles()

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

    # ---------- editor ----------

    def _setup_editor(self):
        self._edit_image = AdjustableImage()
        # The crop rectangle lives in an overlay on top of the canvas so it
        # shares the image's exact allocation and lines up with it.
        self._crop_overlay = CropOverlay()
        self._crop_mode = False
        edit_overlay = Gtk.Overlay(hexpand=True, vexpand=True)
        edit_overlay.set_child(self._edit_image)
        edit_overlay.add_overlay(self._crop_overlay)
        self.edit_image_slot.append(edit_overlay)
        # Slider value (−100..100) -> adjustment. Brightness is additive (±0.5);
        # contrast and saturation are factors around 1.0.
        self.edit_brightness.connect(
            "value-changed", lambda s: self._edit_set("brightness", s.get_value() / 200.0))
        self.edit_contrast.connect(
            "value-changed", lambda s: self._edit_set("contrast", 1.0 + s.get_value() / 100.0))
        self.edit_saturation.connect(
            "value-changed", lambda s: self._edit_set("saturation", 1.0 + s.get_value() / 100.0))
        self.edit_exposure.connect(
            "value-changed", lambda s: self._edit_set("exposure", 1.0 + s.get_value() / 100.0))
        self.edit_temperature.connect(
            "value-changed", lambda s: self._edit_set("temperature", s.get_value() / 100.0))
        # The centre-origin dot on each slider is drawn by EaselAdjustScale
        # itself (see widgets.AdjustScale), so no per-scale setup is needed here.
        self.edit_rotate_left_btn.connect("clicked", lambda *_: self._editor_rotate(-90))
        self.edit_rotate_right_btn.connect("clicked", lambda *_: self._editor_rotate(90))
        self.edit_flip_h_btn.connect("clicked", lambda *_: self._edit_image.toggle_flip("h"))
        self.edit_flip_v_btn.connect("clicked", lambda *_: self._edit_image.toggle_flip("v"))
        self.edit_crop_btn.set_sensitive(True)
        self.edit_crop_btn.set_tooltip_text("Crop")
        self.edit_crop_btn.connect("clicked", lambda *_: self._enter_crop())
        self._build_crop_panel()
        # Each filter chip carries a live thumbnail of the current photo under
        # that filter (set when the editor opens) plus its name below; built into
        # a wrapping flow so the set can grow without overflowing the panel.
        self._edit_filter_btns = {}
        self._edit_filter_thumbs = {}
        for name in self._FILTER_ORDER:
            thumb = FilterThumb(size=54)
            thumb.add_css_class("filter-thumb")
            label = Gtk.Label(label=self._FILTER_LABELS[name],
                              css_classes=["filter-thumb-label"])
            chip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                           halign=Gtk.Align.CENTER)
            chip.append(thumb)
            chip.append(label)
            btn = Gtk.Button(css_classes=["editor-filter"])
            btn.set_child(chip)
            btn.set_cursor(POINTER_CURSOR)
            btn.connect("clicked", lambda _b, n=name: self._apply_filter(n))
            self.edit_filter_flow.insert(btn, -1)
            self._edit_filter_btns[name] = btn
            self._edit_filter_thumbs[name] = thumb
        self.edit_cancel_btn.connect("clicked", lambda *_: self._close_editor())
        self.edit_save_btn.connect("clicked", lambda *_: self._save_edit())

    def _filter_adj(self, name):
        """The adjustment dict a named filter preset represents (slider space ->
        matrix inputs), used to render its preview thumbnail."""
        b, c, s, e, t, tone = self._FILTERS[name]
        return {"brightness": b / 200.0, "contrast": 1.0 + c / 100.0,
                "saturation": 1.0 + s / 100.0, "exposure": 1.0 + e / 100.0,
                "temperature": t / 100.0, "tone": tone,
                "flip_h": False, "flip_v": False, "rotation": 0}

    def _refresh_filter_thumbs(self):
        for name, thumb in getattr(self, "_edit_filter_thumbs", {}).items():
            thumb.set_source(self._edit_texture, self._filter_adj(name))

    def _edit_set(self, name, value):
        if self._edit_image is not None:
            self._edit_image.set_adjustment(name, value)

    @staticmethod
    def _rotate_crop(crop, degrees):
        """Map a normalised (x, y, w, h) crop through a 90° image rotation so it
        keeps framing the same content."""
        if not crop:
            return None
        x, y, w, h = crop
        d = degrees % 360
        if d == 90:    # clockwise
            return (1 - y - h, x, h, w)
        if d == 270:   # counter-clockwise
            return (y, 1 - x - w, h, w)
        if d == 180:
            return (1 - x - w, 1 - y - h, w, h)
        return crop

    def _editor_rotate(self, degrees):
        self._edit_image.rotate(degrees)
        # Rotate the crop with the image so the preview persists. The displayed
        # aspect swaps, so drop any aspect lock (its ratio no longer matches).
        self._applied_crop = self._rotate_crop(self._applied_crop, degrees)
        self._editor_geometry_changed()
        self._crop_overlay.set_aspect_ratio(None)
        self._aspect_label = "Free"
        self._highlight_aspect()
        if self._applied_crop:
            x, y, w, h = self._applied_crop
            self._crop_overlay.set_crop(x, y, x + w, y + h)
        else:
            self._crop_overlay.reset()
        self._edit_image.set_crop(self._applied_crop)

    def _display_dims(self):
        """The displayed image's pixel dimensions (post 90° rotation)."""
        tex = self._edit_texture
        if tex is None:
            return (1.0, 1.0)
        tw, th = tex.get_width(), tex.get_height()
        rot = self._edit_image.adjustments()["rotation"] % 360
        return (th, tw) if rot in (90, 270) else (tw, th)

    def _editor_geometry_changed(self, reset_crop=False):
        """Keep the crop overlay's fit in step with the displayed image (its
        aspect flips on 90/270 rotation)."""
        if self._crop_overlay is None:
            return
        self._crop_overlay.set_display_size(*self._display_dims())
        if reset_crop:
            self._crop_overlay.reset()

    # Common output aspect ratios (width:height); None = free-form.
    _ASPECTS = (("Free", None), ("Original", "original"), ("1:1", (1, 1)),
                ("4:3", (4, 3)), ("3:2", (3, 2)), ("16:9", (16, 9)),
                ("4:5", (4, 5)), ("9:16", (9, 16)))

    def _build_crop_panel(self):
        panel = self.edit_crop_panel
        panel.append(Gtk.Label(label="Aspect Ratio", xalign=0,
                               css_classes=["editor-eyebrow"]))
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                           min_children_per_line=4, max_children_per_line=4,
                           column_spacing=8, row_spacing=8,
                           css_classes=["editor-filters"])
        self._aspect_btns = {}
        self._aspect_label = "Free"
        for label, ratio in self._ASPECTS:
            btn = Gtk.Button(label=label, css_classes=["editor-filter", "aspect-chip"])
            btn.set_cursor(POINTER_CURSOR)
            btn.connect("clicked", lambda _b, l=label, r=ratio: self._set_crop_aspect(l, r))
            flow.insert(btn, -1)
            self._aspect_btns[label] = btn
        panel.append(flow)

        panel.append(Gtk.Label(label="Straighten", xalign=0, margin_top=8,
                               css_classes=["editor-eyebrow"]))
        self._crop_straighten = AdjustScale(
            adjustment=Gtk.Adjustment(lower=-15, upper=15, value=0, step_increment=1),
            draw_value=False, hexpand=True, css_classes=["editor-scale"])
        self._crop_straighten.connect(
            "value-changed", lambda s: self._edit_set("straighten", s.get_value()))
        panel.append(self._crop_straighten)

        buttons = Gtk.Box(spacing=16, margin_top=16, homogeneous=True)
        cancel = Gtk.Button(label="Cancel", css_classes=["editor-cancel"])
        apply_btn = Gtk.Button(label="Apply", css_classes=["editor-save"])
        cancel.set_cursor(POINTER_CURSOR)
        apply_btn.set_cursor(POINTER_CURSOR)
        cancel.connect("clicked", lambda *_: self._exit_crop(False))
        apply_btn.connect("clicked", lambda *_: self._exit_crop(True))
        buttons.append(cancel)
        buttons.append(apply_btn)
        panel.append(buttons)
        self._highlight_aspect()

    def _highlight_aspect(self):
        for label, btn in getattr(self, "_aspect_btns", {}).items():
            if label == self._aspect_label:
                btn.add_css_class("selected")
            else:
                btn.remove_css_class("selected")

    def _set_crop_aspect(self, label, ratio):
        dw, dh = self._display_dims()
        if ratio is None:
            r_norm = None
        elif ratio == "original":
            r_norm = 1.0  # cw/ch = 1 → output aspect == image aspect
        else:
            pw, ph = ratio
            r_norm = (pw / ph) * (dh / dw)
        self._crop_overlay.set_aspect_ratio(r_norm)
        self._aspect_label = label
        self._highlight_aspect()

    def _enter_crop(self):
        self._crop_mode = True
        self._editor_geometry_changed()
        self._crop_backup = (list(self._crop_overlay._crop),
                             self._edit_image.adjustments().get("straighten", 0.0),
                             self._crop_overlay._aspect, self._aspect_label)
        # Show the whole image while cropping so the frame can be placed against
        # all of it; the applied crop is re-shown on exit.
        self._edit_image.set_crop(None)
        self._crop_overlay.set_active(True)
        self.edit_tools.set_visible(False)
        self.edit_crop_panel.set_visible(True)
        self.edit_cancel_btn.set_visible(False)
        self.edit_save_btn.set_visible(False)
        self.edit_crop_btn.add_css_class("selected")
        self._crop_straighten.set_value(
            self._edit_image.adjustments().get("straighten", 0.0))
        self._highlight_aspect()

    def _exit_crop(self, save):
        if save:
            self._applied_crop = self._crop_overlay.get_crop()
        elif getattr(self, "_crop_backup", None) is not None:
            crop, straighten, aspect, label = self._crop_backup
            self._crop_overlay.set_aspect_ratio(aspect, snap=False)
            self._crop_overlay.set_crop(*crop)
            self._edit_image.set_adjustment("straighten", straighten)
            self._crop_straighten.set_value(straighten)
            self._aspect_label = label
            self._highlight_aspect()
        # Show the applied crop live in the canvas.
        self._edit_image.set_crop(self._applied_crop)
        self._crop_backup = None
        self._crop_mode = False
        self._crop_overlay.set_active(False)
        self.edit_tools.set_visible(True)
        self.edit_crop_panel.set_visible(False)
        self.edit_cancel_btn.set_visible(True)
        self.edit_save_btn.set_visible(True)
        self.edit_crop_btn.remove_css_class("selected")

    def _editor_visible(self):
        return self.edit_revealer.get_visible()

    def _open_editor(self, photo_id):
        row = lib.get_photo(self.con, photo_id)
        if not row:
            return
        if lib.is_video(row["path"]):
            self._toast("Videos can't be edited")
            return
        texture = load_full_texture(row["path"])
        if texture is None:
            self._toast("Couldn't open this image for editing")
            return
        self._edit_photo = row["path"]
        self._edit_photo_id = row["id"]
        self._edit_texture = texture
        self._reset_editor()
        self._edit_image.set_texture(texture)
        self._refresh_filter_thumbs()
        # Start from the photo's stored display rotation so the editor matches
        # what's shown everywhere else; saving bakes it into the copy.
        rotation = (row["rotation"] or 0) if "rotation" in row.keys() else 0
        if rotation:
            self._edit_image.set_adjustment("rotation", rotation)
        self._editor_geometry_changed(reset_crop=True)
        self.edit_revealer.set_visible(True)
        self.edit_revealer.set_reveal_child(True)

    # Filter presets: slider positions (−100..100) for
    # brightness/contrast/saturation/exposure/temperature, plus a colour tone.
    _FILTERS = {
        "original": (0, 0, 0, 0, 0, "none"),
        "mono":     (0, 0, -100, 0, 0, "none"),
        "sepia":    (0, 0, 0, 0, 0, "sepia"),
        "warm":     (0, 5, 8, 0, 45, "none"),
        "cool":     (0, 5, -5, 0, -45, "none"),
        "vivid":    (0, 20, 35, 0, 0, "none"),
        "fade":     (8, -25, -20, 0, 5, "none"),
        "noir":     (0, 45, -100, 0, 0, "none"),
    }
    # Display order and labels for the filter chips.
    _FILTER_ORDER = ("original", "mono", "sepia", "warm", "cool", "vivid", "fade", "noir")
    _FILTER_LABELS = {"original": "Original", "mono": "B&W", "sepia": "Sepia",
                      "warm": "Warm", "cool": "Cool", "vivid": "Vivid",
                      "fade": "Fade", "noir": "Noir"}

    def _reset_editor(self):
        for scale in (self.edit_brightness, self.edit_contrast, self.edit_saturation,
                      self.edit_exposure, self.edit_temperature):
            scale.set_value(0)
        if self._edit_image is not None:
            self._edit_image.reset()
        self._applied_crop = None
        self._edit_image.set_crop(None)
        self._crop_overlay.set_aspect_ratio(None)
        self._crop_overlay.reset()
        self._aspect_label = "Free"
        self._highlight_aspect()
        if hasattr(self, "_crop_straighten"):
            self._crop_straighten.set_value(0)
        self._exit_crop_layout()
        self._select_filter("original")

    def _exit_crop_layout(self):
        """Return the editor to adjust mode's layout (used on reset/close)."""
        self._crop_mode = False
        self._crop_backup = None
        self._crop_overlay.set_active(False)
        self.edit_tools.set_visible(True)
        self.edit_crop_panel.set_visible(False)
        self.edit_cancel_btn.set_visible(True)
        self.edit_save_btn.set_visible(True)
        self.edit_crop_btn.remove_css_class("selected")

    def _apply_filter(self, name):
        b, c, s, e, t, tone = self._FILTERS[name]
        self.edit_brightness.set_value(b)
        self.edit_contrast.set_value(c)
        self.edit_saturation.set_value(s)
        self.edit_exposure.set_value(e)
        self.edit_temperature.set_value(t)
        if self._edit_image is not None:
            self._edit_image.set_adjustment("tone", tone)
        self._select_filter(name)

    def _select_filter(self, name):
        for key, btn in getattr(self, "_edit_filter_btns", {}).items():
            btn.set_css_classes(["editor-filter", "selected"] if key == name
                                else ["editor-filter"])

    def _close_editor(self):
        self.edit_revealer.set_reveal_child(False)
        self.edit_revealer.set_visible(False)
        self._exit_crop_layout()
        if self._edit_image is not None:
            self._edit_image.set_texture(None)
        self._edit_texture = None
        self._edit_photo = None
        self._edit_photo_id = None

    def _save_edit(self):
        if self._edit_texture is None or self._edit_photo is None:
            return
        stem = Path(self._edit_photo).stem
        # Saved as PNG: it's the one image encoder that works in the GNOME
        # runtime without gdk-pixbuf (whose JPEG saver fails — its glycin helper
        # can't spawn in the sandbox). PNG is lossless, so no re-compression loss.
        dialog = Gtk.FileDialog(initial_name=f"{stem} (edited).png")
        dialog.save(self, None, self._save_edit_finish)

    def _save_edit_finish(self, dialog, result):
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return
        if not gfile or not gfile.get_path() or self._edit_texture is None:
            return
        dest = gfile.get_path()
        # The output is PNG regardless of the typed name; keep the extension
        # honest so nothing ends up as PNG bytes in a .jpg file.
        if not dest.lower().endswith(".png"):
            dest = os.path.splitext(dest)[0] + ".png"
        adj = self._edit_image.adjustments()
        adj["crop"] = self._applied_crop
        self._toast("Saving edited copy…")
        # GSK rendering isn't thread-safe, so render on the main thread. Defer
        # via idle so the toast paints first; a single image is quick.
        GLib.idle_add(lambda: self._do_save(dest, adj))

    def _do_save(self, dest, adj):
        # GSK rendering isn't thread-safe, so render on the main thread — but the
        # expensive part (PNG encode + file write) runs on a worker so the UI
        # doesn't freeze while a big photo is saved.
        try:
            out = render_adjusted_texture(self._edit_texture, adj)
        except Exception:
            traceback.print_exc()
            out = None
        if out is None:
            print("easel: render_adjusted_texture returned None (save)", file=sys.stderr)
            self._after_save(False, dest)
            return False
        src = self._edit_photo

        def work():
            ok = self._encode_and_write(out, dest, src)
            GLib.idle_add(lambda: self._after_save(ok, dest))

        threading.Thread(target=work, daemon=True).start()
        return False

    def _encode_and_write(self, texture, dest, src):
        # Runs off the main thread. `texture` is an immutable memory texture and
        # `src` a path string, so nothing here touches live UI state.
        try:
            # Texture -> PNG bytes is the same path the thumbnail cache uses and
            # is known to work here; gdk-pixbuf savers are not usable (glycin).
            data = texture.save_to_png_bytes()
            tmp = f"{dest}.{os.getpid()}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(data.get_data())
            os.replace(tmp, dest)
            self._copy_metadata_from(src, dest)
            return True
        except Exception:
            traceback.print_exc()
            return False

    @staticmethod
    def _copy_metadata_from(src, dest):
        """Carry the original photo's EXIF (capture date, camera, GPS…) over to
        the saved PNG copy via an eXIf chunk, so edits don't strip the info the
        library organises by. JPEG source only (that's where EXIF lives);
        best-effort — a failure never affects the already-saved image."""
        if not src or not src.lower().endswith((".jpg", ".jpeg")):
            return
        try:
            seg = lib.read_exif_segment(src)
            tiff = lib.exif_tiff_from_segment(seg) if seg else None
            if tiff:
                lib.write_png_with_exif(dest, tiff)
        except Exception:
            traceback.print_exc()

    def _after_save(self, ok, dest):
        if not ok:
            self._toast("Couldn't save the edited copy")
            return False
        # Offer to show the edited file in place of the original in the library.
        # Both files stay on disk; "Yes" just repoints this library entry (see
        # _on_use_edited). A toast has one button, so Yes shows it and dismissing
        # (swipe / ignore) is "No". Timeout 0 keeps the question until answered.
        if self._edit_photo_id is not None:
            toast = Adw.Toast.new("Image saved. Show it in Easel?")
            toast.set_button_label("Yes")
            toast.set_action_name("win.use-edited")
            toast.set_action_target_value(GLib.Variant(
                "s", json.dumps({"id": self._edit_photo_id, "path": dest})))
            toast.set_timeout(0)
        else:
            toast = Adw.Toast.new("Image saved")
        self.toast_overlay.add_toast(toast)
        self._close_editor()
        return False

    def _on_use_edited(self, _action, param):
        data = json.loads(param.get_string())
        lib.set_photo_path(self.con, data["id"], data["path"])
        self._reload_all()
        if self._info_photo_id == data["id"]:
            self._show_info(data["id"])
        self._toast("Now showing the edited version")

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
        self._stop_lightbox_video()
        self.lightbox_revealer.set_reveal_child(False)
        self.lightbox_revealer.set_visible(False)

    def _stop_lightbox_video(self):
        """Stop and release any playing video so audio doesn't keep going after
        navigating or closing."""
        try:
            stream = self.lightbox_video.get_media_stream()
            if stream is not None:
                stream.pause()
            self.lightbox_video.set_file(None)
        except Exception:
            pass

    def _lightbox_step(self, delta):
        if not self._lightbox_photos:
            return
        self._lightbox_index = (self._lightbox_index + delta) % len(self._lightbox_photos)
        self._show_lightbox_photo()

    def _show_lightbox_photo(self):
        photo = self._lightbox_photos[self._lightbox_index]
        self._stop_lightbox_video()
        if photo.is_video:
            # Play videos with GtkVideo (GStreamer). Guarded so a codec/runtime
            # problem degrades to "nothing plays" rather than taking the app down.
            self.lightbox_picture.set_visible(False)
            self.lightbox_picture.set_paintable(None)
            self.lightbox_video.set_visible(True)
            try:
                self.lightbox_video.set_file(Gio.File.new_for_path(photo.path))
            except Exception:
                self.lightbox_video.set_visible(False)
        else:
            self.lightbox_video.set_visible(False)
            self.lightbox_picture.set_visible(True)
            # Full resolution here (one image at a time). Load the texture
            # ourselves and only ever hand GtkPicture a valid paintable or a
            # clean None — set_filename() leaves the widget in a broken state
            # (non-null content, null paintable) when a file can't be decoded,
            # tripping gtk_scaler_new assertions on every redraw.
            self.lightbox_picture.set_paintable(
                load_full_texture(photo.path, photo.rotation))
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
        self.lightbox_fav_btn.set_icon_name(self._heart_icon(fav))
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
        if self._editor_visible():
            if keyval == Gdk.KEY_Escape:
                self._close_editor()
                return True
            return False
        if not self._lightbox_visible():
            if keyval == Gdk.KEY_Escape and self.info_revealer.get_reveal_child():
                self._close_info()
                return True
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
            remove_btn = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER,
                                    tooltip_text="Disconnect this folder from Easel",
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
            subtitle="Rescan automatically when files in your photo folders change")
        self.settings.bind("watch-folders", watch_row, "active", Gio.SettingsBindFlags.DEFAULT)
        folders.add(watch_row)
        page.add(folders)

        danger = Adw.PreferencesGroup(
            title="Reset",
            description="Easel only reads your folders — disconnecting forgets "
                        "them here but never deletes anything on disk.")
        delete_row = Adw.ActionRow(title="Disconnect All Folders…", activatable=True)
        delete_row.add_css_class("error")
        delete_row.connect("activated", lambda *_: self._confirm_wipe_library(dialog))
        danger.add(delete_row)
        page.add(danger)

        dialog.add(page)
        dialog.present(self)

    def _confirm_remove_folder(self, path, prefs_dialog):
        confirm = Adw.AlertDialog(
            heading="Disconnect folder?",
            body=f"Easel will stop showing photos from “{path}” and forget its "
                 "favourites and people tags. The folder and your photos on disk "
                 "are not touched — you can reconnect it any time.")
        confirm.add_response("cancel", "Cancel")
        confirm.add_response("disconnect", "Disconnect")
        confirm.set_response_appearance("disconnect", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, response):
            if response != "disconnect":
                return
            lib.remove_folder(self.con, path)
            self._reload_all()
            self._refresh_watchers()
            self._toast("Folder disconnected")
            prefs_dialog.close()

        confirm.connect("response", on_response)
        confirm.present(self)

    def _confirm_wipe_library(self, prefs_dialog):
        confirm = Adw.AlertDialog(
            heading="Disconnect all folders?",
            body="Easel will forget every folder you've added, along with all "
                 "favourites and people tags. Your photo files on disk are not "
                 "touched — nothing is deleted.")
        confirm.add_response("cancel", "Cancel")
        confirm.add_response("disconnect", "Disconnect All")
        confirm.set_response_appearance("disconnect", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, response):
            if response != "disconnect":
                return
            lib.wipe_library(self.con)
            self._close_info()
            self._reload_all()
            self._refresh_watchers()
            self._toast("All folders disconnected")
            prefs_dialog.close()

        confirm.connect("response", on_response)
        confirm.present(self)

    def _setup_watching(self):
        self._monitors = []
        self._watch_debounce = 0
        self.settings.connect("changed::watch-folders", lambda *_: self._refresh_watchers())
        self._refresh_watchers()

    # ---------- importing (files + drag-and-drop) ----------

    def _setup_dnd(self):
        """Accept photos (and folders) dropped anywhere on the window."""
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

    def _on_drop(self, _target, value, _x, _y):
        try:
            files = value.get_files()
        except Exception:
            return False
        paths = [f.get_path() for f in files if f and f.get_path()]
        if not paths:
            return False
        self._import_paths(paths)
        return True

    def _import_paths(self, paths):
        """Index dropped files: media files individually, folders as watched
        photo folders. Runs off the main thread with a progress toast."""
        if not paths:
            return

        def scan(progress):
            for path in paths:
                if os.path.isdir(path):
                    lib.add_folder(self.con, path)
                    lib.scan_folder(self.con, path, progress)
                elif os.path.splitext(path)[1].lower() in lib.MEDIA_EXT:
                    lib.scan_file(self.con, path)

        self._run_scan(scan, "Importing…", refresh_watchers=True)

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

    def _on_show_hidden(self, action, _param):
        self._show_hidden = not self._show_hidden
        action.set_state(GLib.Variant("b", self._show_hidden))
        self._reload_all()

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

    def _select_tab(self, name, close_panel=True):
        # Switching tabs drops any selected photo and closes the info panel
        # (internal re-renders pass close_panel=False so a background reload
        # doesn't yank an open panel away).
        if close_panel and self.info_revealer.get_reveal_child():
            self._close_info()
        self.view = name
        self._last_tab = name
        if not self._photos_all and name in ("all_photos", "months", "years", "favourites"):
            self.paper_stack.set_visible_child_name("empty")
        else:
            self.paper_stack.set_visible_child_name(name)
            if name == "months":
                self._render_months()
            elif name == "years":
                self._render_years()
            elif name == "map":
                self._render_map()
            elif name == "people":
                self._render_people()
        self.detail_back_row.set_visible(False)
        self._update_sort_button()
        for key, btn in self._tab_buttons.items():
            if key == name:
                btn.add_css_class("tab-active")
            else:
                btn.remove_css_class("tab-active")

    def _activate_album_item(self, item):
        """A card in the Albums grid (or a sub-folder card) was clicked: drill
        into a folder node, or open a user-created album."""
        if item is None:
            return
        if item.folder:
            self._open_folder(item.path)
        else:
            self._open_album(item.id)

    def _open_album(self, album_id):
        if not lib.get_album(self.con, album_id):
            return
        self._open_detail(("album", album_id))

    def _open_folder(self, path):
        if path not in (self._folder_nodes or {}):
            return
        self._open_detail(("folder", path))

    def _open_period(self, kind, key, title):
        self._open_detail(("period", kind, key, title))

    def _open_detail(self, source):
        self.view = "detail"
        self._detail_source = source
        self.paper_stack.set_visible_child_name("detail")
        self.detail_back_row.set_visible(True)
        self.sort_btn.set_visible(False)
        self._render_detail()

    def _go_back(self):
        # Inside the folder tree, "back" climbs to the parent folder; at a root
        # (or any other detail) it returns to the tab you came from.
        source = self._detail_source
        if source and source[0] == "folder":
            node = (self._folder_nodes or {}).get(source[1])
            parent = node["parent"] if node else None
            if parent and parent in self._folder_nodes:
                self._open_folder(parent)
                return
            self._select_tab("albums")
            return
        self._select_tab(self._last_tab if self._last_tab in VIEW_NAMES else "all_photos")

    def _clear_box(self, box):
        child = box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    def _render_detail(self):
        source = self._detail_source
        if not source:
            self._go_back()
            return
        subfolders = []       # folder cards shown above the photos (tree view)
        extra_stats = []      # extra "N folders" style stats
        if source[0] == "album":
            album = lib.get_album(self.con, source[1])
            if not album:
                self._go_back()
                return
            kind_label, title = "Album", album["title"]
            cover = album["cover_path"] or None
            photos = [self._photo_from_row(r)
                      for r in lib.photos_by_album(self.con, album["id"],
                                                   include_hidden=self._show_hidden)]
            date = _fmt_date(album["date_taken"])
        elif source[0] == "folder":
            node = (self._folder_nodes or {}).get(source[1])
            if not node:
                self._go_back()
                return
            kind_label, title = "Folder", node["title"]
            cover = node["cover"] or None
            date = ""
            subfolders = self._folder_child_items(node)
            if node["album_id"]:
                photos = [self._photo_from_row(r)
                          for r in lib.photos_by_album(self.con, node["album_id"],
                                                        include_hidden=self._show_hidden)]
            else:
                photos = []
            # Stats describe the whole subtree; the photos grid shows only the
            # photos that live directly in this folder, sub-folders hold the rest.
            total = node["total"]
            self._detail_photos = photos
            self._render_subfolders(subfolders)
            self.detail_kind_label.set_label(kind_label)
            self._clear_box(self.detail_hero_slot)
            hero = Swatch("album", size=108)
            hero.set_path(cover)
            self.detail_hero_slot.append(hero)
            self.detail_name_label.set_label(title)
            parts = [f"{total} photo{'s' if total != 1 else ''}"]
            if subfolders:
                k = len(subfolders)
                parts.append(f"{k} folder{'s' if k != 1 else ''}")
            self.detail_stats_label.set_label(" · ".join(parts))
            self._fill_store(self.detail_store, self._detail_photos)
            self._schedule_resize_tiles()
            return
        elif source[0] == "person":
            person = lib.get_person(self.con, source[1])
            if not person:
                self._go_back()
                return
            kind_label, title = "Person", person["name"]
            photos = [self._photo_from_row(r)
                      for r in lib.photos_for_person(self.con, person["id"],
                                                     include_hidden=self._show_hidden)]
            cover = photos[0].path if photos else None
            date = ""
        else:  # ("period", kind, key, title)
            _, kind, key, title = source
            kind_label = "Month" if kind == "month" else "Year"
            photos = [p for p in self._visible_photos
                      if self._period_of(p.date_taken, kind)[0] == key]
            photos.sort(key=lambda p: p.date_taken)
            cover = photos[-1].path if photos else None
            date = ""

        self._render_subfolders(subfolders)  # empty for non-folder detail: hides it
        self.detail_kind_label.set_label(kind_label)
        self._clear_box(self.detail_hero_slot)
        hero = Swatch(kind_label.lower(), size=108)
        hero.set_path(cover)
        self.detail_hero_slot.append(hero)
        self.detail_name_label.set_label(title)

        count = len(photos)
        parts = [f"{count} photo{'s' if count != 1 else ''}"]
        if date:
            parts.append(date)
        self.detail_stats_label.set_label(" · ".join(parts))

        self._detail_photos = photos
        self._fill_store(self.detail_store, self._detail_photos)
        self._schedule_resize_tiles()

    def _folder_child_items(self, node):
        items = []
        for child_path in node["children"]:
            cn = self._folder_nodes.get(child_path)
            if not cn:
                continue
            items.append(Album(id=cn["album_id"] or 0, title=cn["title"],
                               path=cn["path"], photo_count=cn["total"],
                               cover_path=cn["cover"] or "", folder=True,
                               subfolder_count=len(cn["children"])))
        return items

    def _render_subfolders(self, items):
        self._fill_store(self.detail_folders_store, items)
        self.detail_folders_grid.set_visible(bool(items))

    # ---------- map view ----------

    def _render_map(self):
        """Pin every geotagged photo on the offline world map. The first time
        it's opened in a session we also read GPS from any photo that hasn't
        been checked yet (a background pass), then refresh the pins."""
        rows = lib.photos_with_location(self.con, include_hidden=self._show_hidden)
        entries = [(r["lon"], r["lat"], self._photo_from_row(r)) for r in rows]
        self._map_view.set_photos(entries)
        self.map_stack.set_visible_child_name("view" if entries else "empty")
        self._maybe_backfill_locations()

    def _maybe_backfill_locations(self):
        if self._gps_backfilled or not self._photos_all:
            return
        self._gps_backfilled = True

        def work():
            found = lib.backfill_locations(self.con)
            if found:
                GLib.idle_add(self._refresh_map_if_current)

        threading.Thread(target=work, daemon=True).start()

    def _refresh_map_if_current(self):
        if self.view == "map":
            self._render_map()
        return False

    def _on_map_pin(self, photos):
        """A map pin was clicked: open its photos in the lightbox."""
        if photos:
            self._open_lightbox(photos, 0)

    # ---------- people view ----------

    def _render_people(self):
        self._fill_store(self.people_store, self._persons_all)
        self.people_stack.set_visible_child_name(
            "view" if self._persons_all else "empty")

    def _bind_person_card(self, item):
        person = item.get_item()
        box = item.get_child()
        if not hasattr(box, "swatch"):
            box = self._album_card_widget()  # same card, cover styled by .card-cover
            item.set_child(box)
        box.swatch.set_size(self._card_cell_px())
        box.swatch.set_placeholder("person")
        box.swatch.set_path(person.cover_path or None)
        box.title.set_label(person.name)
        count = person.photo_count
        box.subtitle.set_label(f"{count} photo{'s' if count != 1 else ''}")
        self._attach_person_menu(box, person.id)

    def _attach_person_menu(self, box, person_id):
        box._menu_kind = "person"
        box._menu_item_id = person_id
        box._menu_entries = PERSON_ENTRIES
        box._menu_extra = {}
        if getattr(box, "_person_menu_attached", False):
            return
        box._person_menu_attached = True
        gesture = Gtk.GestureClick(button=3)
        gesture.connect("pressed", lambda _g, _n, x, y: self._show_item_menu(box, box, x, y))
        box.add_controller(gesture)

    def _open_person(self, person_id):
        if not lib.get_person(self.con, person_id):
            return
        self._open_detail(("person", person_id))

    # ---------- search / filters ----------

    def _on_search_changed(self, entry):
        self._search_query = entry.get_text().strip().lower()
        self._apply_filters()

    def _sorted_photos(self, photos):
        if self._sort["photos"] == "date-asc":
            return sorted(photos, key=lambda p: p.date_taken)
        return sorted(photos, key=lambda p: -p.date_taken)

    def _sorted_albums(self, albums):
        mode = self._sort["albums"]
        if mode == "date":
            return sorted(albums, key=lambda a: -a.date_taken)
        if mode == "count":
            return sorted(albums, key=lambda a: (-a.photo_count, a.title.lower()))
        return sorted(albums, key=lambda a: a.title.lower())

    def _photo_datetext(self, p):
        """Searchable date text for a photo: year plus month name (full and
        abbreviated), so "2014", "march" and "mar" all match."""
        if not p.date_taken:
            return ""
        try:
            dt = datetime.fromtimestamp(p.date_taken)
        except (ValueError, OSError):
            return ""
        return f"{dt.strftime('%Y')} {dt.strftime('%B')} {dt.strftime('%b')}".lower()

    def _photo_matches(self, p, q):
        if not q:
            return True
        if q in os.path.basename(p.path).lower():
            return True
        if q in (p.album or "").lower():
            return True
        # People tagged in the photo.
        for name in self._photo_people.get(p.id, ()):
            if q in name.lower():
                return True
        # Date: year / month name.
        return q in self._photo_datetext(p)

    @staticmethod
    def _fill_store(store, items):
        # One splice instead of N appends: the model emits a single
        # items-changed, so the grid updates once even for thousands of photos
        # (per-item appends made loading a big library crawl).
        store.splice(0, store.get_n_items(), list(items))

    def _apply_filters(self):
        q = self._search_query

        self._visible_photos = [p for p in self._sorted_photos(self._photos_all)
                                if self._photo_matches(p, q)]
        self._fill_store(self.photo_store, self._visible_photos)

        albums = [a for a in self._sorted_albums(self._albums_all)
                  if not q or q in a.title.lower()]
        self._fill_store(self.album_store, albums)

        self._visible_favs = [p for p in self._visible_photos if p.favorite]
        self._fill_store(self.fav_store, self._visible_favs)

    # ---------- library loading ----------

    def _on_add_folder(self):
        dialog = Gtk.FileDialog()
        dialog.select_folder(self, None, self._folder_chosen)

    def _run_scan(self, scan_fn, start_msg, refresh_watchers=False):
        """Run a scan on a worker thread while a single, persistent toast shows
        live progress ("Scanning… 1,234 of 7,000"), so a big import never looks
        stuck. scan_fn(progress_cb) does the work; progress_cb(done, total) is
        called from the worker and throttled onto the main thread."""
        toast = Adw.Toast.new(start_msg)
        toast.set_timeout(0)  # stays until we dismiss it
        self.toast_overlay.add_toast(toast)
        state = {"last": 0.0}

        def progress(done, total):
            now = time.monotonic()
            if not total or (done < total and now - state["last"] < 0.1):
                return
            state["last"] = now
            GLib.idle_add(
                lambda: toast.set_title(f"Scanning… {done:,} of {total:,}") or False)

        def work():
            scan_fn(progress)

            def finish():
                toast.dismiss()
                self._reload_all()
                if refresh_watchers:
                    self._refresh_watchers()
                self._toast_photo_count()
                return False

            GLib.idle_add(finish)

        threading.Thread(target=work, daemon=True).start()

    def _on_rescan(self):
        self._run_scan(lambda cb: lib.scan_all(self.con, cb), "Rescanning library…")

    def _toast_photo_count(self):
        self._toast(f"Library updated — {len(self._photos_all):,} photos")
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
        self._run_scan(lambda cb: lib.scan_folder(self.con, path, cb),
                       "Scanning folder…", refresh_watchers=True)

    def _photo_from_row(self, r):
        return Photo(id=r["id"], path=r["path"], album=r["album_title"] or "",
                     date_taken=r["date_taken"] or 0.0, favorite=bool(r["favorite"]),
                     is_video=lib.is_video(r["path"]),
                     rotation=(r["rotation"] or 0) if "rotation" in r.keys() else 0)

    def _reload_all(self):
        self._photos_all = [self._photo_from_row(r)
                            for r in lib.all_photos(self.con, include_hidden=self._show_hidden)]
        # The folder tree drives the Albums view: it shows each watched root as a
        # folder card (drill in to browse sub-folders), alongside user-created
        # albums. "Add to" menus offer only the user albums (folders are physical).
        self._folder_nodes, self._folder_roots = lib.folder_tree(self.con)
        self._user_albums = [
            Album(id=r["id"], title=r["title"], path="",
                  photo_count=r["photo_count"] or 0, cover_path=r["cover_path"] or "",
                  date_taken=r["date_taken"] or 0.0, folder=False)
            for r in lib.all_albums(self.con) if not r["path"]
        ]
        root_cards = []
        for path in self._folder_roots:
            node = self._folder_nodes[path]
            root_cards.append(Album(
                id=node["album_id"] or 0, title=node["title"], path=node["path"],
                photo_count=node["total"], cover_path=node["cover"] or "",
                folder=True, subfolder_count=len(node["children"])))
        self._albums_all = root_cards + self._user_albums
        self._persons_all = [
            Person(id=r["id"], name=r["name"], photo_count=r["photo_count"] or 0,
                   cover_path=r["cover_path"] or "", date_taken=r["date_taken"] or 0.0)
            for r in lib.all_persons(self.con)
        ]
        self._photo_people = lib.people_by_photo(self.con)
        self._apply_filters()
        if self.view == "detail" and self._detail_source is not None:
            self._render_detail()
        elif self.view in VIEW_NAMES:
            self._select_tab(self.view, close_panel=False)
        self._schedule_resize_tiles()
        return False
