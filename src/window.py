"""Lyre's main window.

Visual design follows the Tempo-inspired mockup, reworked per follow-up
feedback: a flat grey desktop, a "paper" card holding the library (Artists /
Albums / Tracks, switchable from a Dialect-style dropdown in the titlebar),
and a player panel that floats on the grey background and pushes the paper
aside while something is playing.
"""
import json
import os
import shutil
import threading
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from . import library as lib
from . import metadata as meta
from .models import Album, Artist, Playlist, Track
from .mpris import MprisServer
from .player import Player, Queue
from .widgets import Swatch

APP_ID = "io.github.drvonmiau.Lyre"

TRACK_ENTRIES = [
    ("Play", "play"), ("Play next", "play-next"), ("Play last", "play-last"),
    (None, None),
    ("Show artist", "show-artist"), ("Show album", "show-album"),
    ("Add to Favourites", "toggle-fav"),
    ("Add to Playlist", "__playlists__"),
    (None, None),
    ("Edit Metadata…", "edit-meta"),
    ("Delete from library", "delete"),
]
PLAYLIST_TRACK_ENTRIES = [
    ("Play", "play"), ("Play next", "play-next"), ("Play last", "play-last"),
    (None, None),
    ("Show artist", "show-artist"), ("Show album", "show-album"),
    ("Add to Favourites", "toggle-fav"),
    ("Remove from this Playlist", "remove-from-playlist"),
    (None, None),
    ("Edit Metadata…", "edit-meta"),
    ("Delete from library", "delete"),
]
PLAYLIST_ENTRIES = [
    ("Play", "play"), ("Play next", "play-next"), ("Play last", "play-last"),
    (None, None),
    ("Rename…", "rename-playlist"),
    (None, None),
    ("Delete playlist", "delete"),
]
ALBUM_ENTRIES = [
    ("Play", "play"), ("Play next", "play-next"), ("Play last", "play-last"),
    (None, None),
    ("Show artist", "show-artist"), ("Set cover image…", "set-image"),
    (None, None),
    ("Edit Album…", "edit-album"),
    ("Delete from library", "delete"),
]
ARTIST_ENTRIES = [
    ("Play", "play"), ("Play next", "play-next"), ("Play last", "play-last"),
    (None, None),
    ("Set artist image…", "set-image"),
    (None, None),
    ("Rename Artist…", "rename-artist"),
    ("Remove from library", "delete"),
]

THEME_SCHEMES = {
    "light": Adw.ColorScheme.FORCE_LIGHT,
    "dark": Adw.ColorScheme.FORCE_DARK,
    "system": Adw.ColorScheme.DEFAULT,
}

VIEW_NAMES = ("artists", "albums", "tracks", "favourites", "playlists")

# Fixed spacing scale (px). Every hand-set gap in the app should use one of
# these; dynamic spacing (paper margins, paper-player gap) lives in
# _apply_layout_metrics. Documented in the project styleguide.
SPACE_XS, SPACE_S, SPACE_M, SPACE_L, SPACE_XL = 4, 8, 16, 24, 32

# Web-style hand cursor for anything clickable.
POINTER_CURSOR = Gdk.Cursor.new_from_name("pointer")

# Sort options per tab group (favourites shares the tracks group).
SORT_OPTIONS = {
    "artists": [("Name", "name"), ("Most played", "plays")],
    "albums": [("Artist", "artist"), ("Title", "title"), ("Year", "year"),
               ("Most played", "plays")],
    "tracks": [("Title", "title"), ("Artist", "artist"), ("Album", "album"),
               ("Most played", "plays"), ("Recently added", "recent")],
}
SORT_GROUP_FOR_TAB = {"artists": "artists", "albums": "albums",
                      "tracks": "tracks", "favourites": "tracks"}



def _fmt_time(seconds):
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


@Gtk.Template(resource_path="/io/github/drvonmiau/Lyre/window.ui")
class MusicWindow(Adw.ApplicationWindow):
    __gtype_name__ = "MusicWindow"

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
    tab_artists = Gtk.Template.Child()
    tab_albums = Gtk.Template.Child()
    tab_tracks = Gtk.Template.Child()
    tab_favourites = Gtk.Template.Child()
    tab_playlists = Gtk.Template.Child()
    search_entry = Gtk.Template.Child()

    paper_stack = Gtk.Template.Child()
    artist_grid = Gtk.Template.Child()
    album_grid = Gtk.Template.Child()
    track_list = Gtk.Template.Child()
    fav_list = Gtk.Template.Child()
    playlist_grid = Gtk.Template.Child()

    detail_back_row = Gtk.Template.Child()
    back_btn = Gtk.Template.Child()
    detail_kind_label = Gtk.Template.Child()
    detail_hero_slot = Gtk.Template.Child()
    detail_name_label = Gtk.Template.Child()
    detail_stats_label = Gtk.Template.Child()
    detail_albums_section = Gtk.Template.Child()
    detail_albums_box = Gtk.Template.Child()
    detail_filter_label = Gtk.Template.Child()
    detail_tracks_box = Gtk.Template.Child()

    player_revealer = Gtk.Template.Child()
    player_panel = Gtk.Template.Child()
    player_art_slot = Gtk.Template.Child()
    now_title = Gtk.Template.Child()
    now_artist = Gtk.Template.Child()
    seek_scale = Gtk.Template.Child()
    elapsed_label = Gtk.Template.Child()
    duration_label = Gtk.Template.Child()
    play_btn = Gtk.Template.Child()
    play_icon = Gtk.Template.Child()
    prev_btn = Gtk.Template.Child()
    next_btn = Gtk.Template.Child()
    shuffle_btn = Gtk.Template.Child()
    repeat_btn = Gtk.Template.Child()
    volume_btn = Gtk.Template.Child()
    volume_scale = Gtk.Template.Child()
    upnext_header = Gtk.Template.Child()
    upnext_clear_btn = Gtk.Template.Child()
    upnext_box = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.con = lib.connect()
        self.queue = Queue()
        self.player = Player(on_eos=self._advance, on_error=self._on_player_error,
                             on_stream_start=self._on_gapless_started)
        self.player.set_gapless_source(self._gapless_next_path)
        self.settings = Gio.Settings.new(APP_ID)
        self._gapless_pending = None
        self._inhibit_cookie = 0
        self._sleep_source = None

        self.view = "artists"
        self._last_tab = "artists"
        self._detail_mode = "artist"  # artist | album | playlist
        self._detail_artist_id = None
        self._detail_album_id = None
        self._detail_playlist_id = None
        self._playlists_all = []
        self._detail_album_filter = None
        self._detail_album_ids = []
        self._detail_tracks = []
        self._player_art = None
        self._search_query = ""
        self._artists_all = []
        self._albums_all = []
        self._tracks_all = []
        self._visible_tracks = []
        self._flowbox_connected = False
        self._surface_width = 0
        self._surface_height = 0

        self._visible_favs = []
        self._sort = {group: self.settings.get_string(f"sort-{group}")
                      for group in SORT_OPTIONS}
        self._track_plays = {}
        self._artist_plays = {}
        self._album_plays = {}

        self._tab_buttons = {
            "artists": self.tab_artists,
            "albums": self.tab_albums,
            "tracks": self.tab_tracks,
            "favourites": self.tab_favourites,
            "playlists": self.tab_playlists,
        }

        self._setup_actions()
        self._setup_window_controls()
        self._setup_lists()
        self._setup_player_controls()
        self._setup_help_overlay()

        for key, btn in self._tab_buttons.items():
            btn.connect("clicked", lambda _b, k=key: self._select_tab(k))
        self.back_btn.connect("clicked", lambda *_: self._go_back())
        self.search_entry.connect("search-changed", self._on_search_changed)

        self.connect("realize", self._on_realize)
        self.connect("close-request", self._on_close_request)

        GLib.timeout_add(200, self._tick)
        self._setup_theme()
        self._restore_state()
        self._reload_all()
        self._restore_queue()
        self._setup_watching()
        self.mpris = MprisServer(self)
        self._setup_titlebar_sides()
        self._apply_pointer_cursors()

    @staticmethod
    def _close_button_is_left(layout):
        """True if the system's decoration layout puts the close button on
        the left half (e.g. "close,minimize,maximize:" as on macOS-style
        setups)."""
        left = (layout or "").split(":")[0]
        return "close" in left

    def _setup_titlebar_sides(self):
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.connect("notify::gtk-decoration-layout",
                             lambda *_a: self._apply_titlebar_side())
        self._apply_titlebar_side()

    def _apply_titlebar_side(self):
        """Keep the volume + menu group on the OPPOSITE side of the window
        controls, whichever side the system (or a theme switch) puts them."""
        settings = Gtk.Settings.get_default()
        layout = settings.get_property("gtk-decoration-layout") if settings else ""
        aux = (self.volume_btn, self.volume_scale, self.menu_button)
        box = self.titlebar_box
        if self._close_button_is_left(layout):
            # window buttons on the left -> aux group to the right
            box.reorder_child_after(self.titlebar_spacer, self.wc_start)
            previous = self.titlebar_spacer
        else:
            # window buttons on the right (GNOME default) -> aux stays left
            previous = self.wc_start
        for widget in aux:
            box.reorder_child_after(widget, previous)
            previous = widget
        if not self._close_button_is_left(layout):
            box.reorder_child_after(self.titlebar_spacer, previous)

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

    def current_cover_path(self):
        t = self.queue.current
        if not t or not t.album_id:
            return None
        album = lib.get_album(self.con, t.album_id)
        return (album["cover_path"] if album else None) or None

    # ---------- remembered state ----------

    def _restore_state(self):
        self.set_default_size(self.settings.get_int("window-width"),
                              self.settings.get_int("window-height"))
        if self.settings.get_boolean("window-maximized"):
            self.maximize()
        self.volume_scale.set_value(self.settings.get_double("volume"))
        self.shuffle_btn.set_active(self.settings.get_boolean("shuffle"))
        self.repeat_btn.set_active(self.settings.get_boolean("repeat"))
        saved_tab = self.settings.get_string("last-tab")
        self._select_tab(saved_tab if saved_tab in VIEW_NAMES else "artists")

    def _restore_queue(self):
        """Bring back the last session's queue, paused on the saved track."""
        raw = self.settings.get_string("queue")
        if not raw:
            return
        try:
            data = json.loads(raw)
        except ValueError:
            return
        ids = [data.get("current")] + list(data.get("upcoming", []))
        tracks = []
        for track_id in ids:
            row = lib.get_track(self.con, track_id) if track_id else None
            if row:
                tracks.append(Track(
                    id=row["id"], path=row["path"], title=row["title"],
                    artist=row["artist_name"], album=row["album_title"],
                    album_id=row["album_id"], track_no=row["track_no"] or 0,
                    duration=row["duration"] or 0.0))
        if not tracks:
            return
        self.queue.play(tracks)
        t = self.queue.current
        self.player.load(t.path)
        self.now_title.set_label(t.title)
        self.now_artist.set_label(t.artist)
        if self._player_art is None:
            self._player_art = Swatch("cover art", size=self.PLAYER_WIDTH)
            self._player_art.set_hexpand(True)
            self.player_art_slot.append(self._player_art)
        album = lib.get_album(self.con, t.album_id) if t.album_id else None
        self._player_art.set_path((album["cover_path"] if album else None) or None)
        self._set_player_revealed(True)
        self.play_icon.set_from_icon_name("lyre-play-symbolic")
        self._refresh_upnext()

    def _on_close_request(self, *_args):
        self.settings.set_boolean("window-maximized", self.is_maximized())
        if not self.is_maximized():
            width, height = self.get_default_size()
            self.settings.set_int("window-width", width)
            self.settings.set_int("window-height", height)
        self.settings.set_double("volume", self.volume_scale.get_value())
        self.settings.set_boolean("shuffle", self.shuffle_btn.get_active())
        self.settings.set_boolean("repeat", self.repeat_btn.get_active())
        self.settings.set_string("last-tab",
                                 self._last_tab if self._last_tab in VIEW_NAMES else "artists")
        if self.queue.current:
            self.settings.set_string("queue", json.dumps({
                "current": self.queue.current.id,
                "upcoming": [t.id for t in self.queue.upcoming],
            }))
        else:
            self.settings.set_string("queue", "")
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

    PLAYER_WIDTH = 300

    def _apply_layout_metrics(self):
        """5% top/left/right margins, paper flush to the window bottom, 5% gap,
        player fixed at 300px. On wide windows the margins absorb the extra
        space so the paper + player block stays centered."""
        width, height = self._surface_width, self._surface_height
        if width <= 0 or height <= 0:
            return
        margin_y = round(height * 0.05)
        margin_x = max(SPACE_L, round(width * 0.05))
        revealed = self.player_revealer.get_reveal_child()
        if revealed:
            gap = round(width * 0.05)
            ideal_paper = round(width * 0.60)
            centered = (width - ideal_paper - gap - self.PLAYER_WIDTH) // 2
            margin_x = max(margin_x, centered)
        else:
            gap = 0
        self.content_row.set_margin_start(margin_x)
        self.content_row.set_margin_end(margin_x)
        # The nav band supplies the paper's top gap (fixed spacing tokens).
        self.content_row.set_margin_top(0)
        self.content_row.set_margin_bottom(0)
        # The nav band always spans exactly the paper: tabs at the paper's
        # left edge, sort/search at its right — even when the player panel
        # is out (its width + gap are added to the end margin).
        self.nav_row.set_margin_start(margin_x)
        self.nav_row.set_margin_end(margin_x + (gap + self.PLAYER_WIDTH if revealed else 0))
        self.player_panel.set_size_request(self.PLAYER_WIDTH if revealed else 0, -1)
        self.player_revealer.set_margin_start(gap)
        self.player_revealer.set_margin_bottom(margin_y)
        if self._player_art is not None:
            self._player_art.set_size(self.PLAYER_WIDTH)

    def _set_player_revealed(self, revealed):
        self.player_revealer.set_reveal_child(revealed)
        self._apply_layout_metrics()

    def _setup_help_overlay(self):
        builder = Gtk.Builder.new_from_resource("/io/github/drvonmiau/Lyre/gtk/help-overlay.ui")
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

        fetch_metadata = Gio.SimpleAction.new("fetch-metadata", None)
        fetch_metadata.connect("activate", lambda *_a: self._on_fetch_metadata())
        self.add_action(fetch_metadata)

        new_playlist = Gio.SimpleAction.new("new-playlist", None)
        new_playlist.connect("activate", lambda *_a: self._on_new_playlist())
        self.add_action(new_playlist)

        preferences = Gio.SimpleAction.new("preferences", None)
        preferences.connect("activate", lambda *_a: self._on_preferences())
        self.add_action(preferences)

        play_pause = Gio.SimpleAction.new("play-pause", None)
        play_pause.connect("activate", lambda *_a: self._toggle_play())
        self.add_action(play_pause)

        next_track = Gio.SimpleAction.new("next-track", None)
        next_track.connect("activate", lambda *_a: self._advance())
        self.add_action(next_track)

        prev_track = Gio.SimpleAction.new("prev-track", None)
        prev_track.connect("activate", lambda *_a: self._on_prev())
        self.add_action(prev_track)

        find = Gio.SimpleAction.new("find", None)
        find.connect("activate", lambda *_a: self.search_toggle_btn.set_active(
            not self.search_toggle_btn.get_active()))
        self.add_action(find)

        volume_up = Gio.SimpleAction.new("volume-up", None)
        volume_up.connect("activate", lambda *_a: self._volume_step(0.05))
        self.add_action(volume_up)

        volume_down = Gio.SimpleAction.new("volume-down", None)
        volume_down.connect("activate", lambda *_a: self._volume_step(-0.05))
        self.add_action(volume_down)

        mute = Gio.SimpleAction.new("mute", None)
        mute.connect("activate", lambda *_a: self._toggle_mute())
        self.add_action(mute)

        for i, tab in enumerate(VIEW_NAMES, start=1):
            act = Gio.SimpleAction.new(f"tab-{i}", None)
            act.connect("activate", lambda *_a, t=tab: self._select_tab(t))
            self.add_action(act)

        app = self.get_application()
        if app is not None:
            # NOTE: space is deliberately NOT a global accelerator - a global
            # accel fires even while typing in the search box (the entry never
            # receives the key). Instead a BUBBLE-phase key controller below
            # toggles play/pause only when no text field consumed the press.
            app.set_accels_for_action("win.next-track", ["<primary>Right"])
            app.set_accels_for_action("win.prev-track", ["<primary>Left"])
            app.set_accels_for_action("win.find", ["<primary>f"])
            app.set_accels_for_action("win.volume-up", ["<primary>Up"])
            app.set_accels_for_action("win.volume-down", ["<primary>Down"])
            app.set_accels_for_action("win.mute", ["<primary>m"])
            for i in range(1, len(VIEW_NAMES) + 1):
                app.set_accels_for_action(f"win.tab-{i}", [f"<primary>{i}"])

        # Space toggles play/pause everywhere EXCEPT while typing in a text
        # field. CAPTURE phase runs before the focused widget: buttons/cards
        # never get to treat space as a click (Enter still activates them),
        # and _on_space_pressed steps aside when the focus is editable text.
        space_ctl = Gtk.EventControllerKey()
        space_ctl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        space_ctl.connect("key-pressed", self._on_space_pressed)
        self.add_controller(space_ctl)

        sort_mode = Gio.SimpleAction.new_stateful(
            "sort-mode", GLib.VariantType.new("s"),
            GLib.Variant("s", self._sort["artists"]))
        sort_mode.connect("activate", self._on_sort_mode)
        self.add_action(sort_mode)

        sleep_timer = Gio.SimpleAction.new_stateful(
            "sleep-timer", GLib.VariantType.new("i"), GLib.Variant("i", 0))
        sleep_timer.connect("activate", self._on_sleep_timer)
        self.add_action(sleep_timer)

        item_actions = Gio.SimpleActionGroup()
        for name in ("play", "play-next", "play-last", "show-artist", "show-album",
                     "set-image", "toggle-fav", "add-to-playlist", "add-to-new-playlist",
                     "remove-from-playlist", "rename-playlist", "edit-meta",
                     "edit-album", "rename-artist", "delete"):
            act = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            act.connect("activate", self._on_item_action)
            item_actions.add_action(act)
        self.insert_action_group("item", item_actions)

    # ---------- list/grid setup ----------

    def _setup_lists(self):
        self.artist_store = Gio.ListStore(item_type=Artist)
        self.artist_grid.set_model(Gtk.SingleSelection(model=self.artist_store))
        self.artist_grid.set_factory(self._factory(self._bind_artist_card))
        self.artist_grid.set_single_click_activate(True)
        self.artist_grid.connect(
            "activate", lambda g, p: self._open_artist(g.get_model().get_item(p).id)
        )

        self.album_store = Gio.ListStore(item_type=Album)
        self.album_grid.set_model(Gtk.SingleSelection(model=self.album_store))
        self.album_grid.set_factory(self._factory(self._bind_album_card))
        self.album_grid.set_single_click_activate(True)
        self.album_grid.connect(
            "activate", lambda g, p: self._open_album(g.get_model().get_item(p).id)
        )

        self.track_store = Gio.ListStore(item_type=Track)
        self.track_list.set_model(Gtk.NoSelection(model=self.track_store))
        self.track_list.set_factory(self._factory(self._bind_track_row))
        self.track_list.connect("activate", self._on_track_activate)

        self.fav_store = Gio.ListStore(item_type=Track)
        self.fav_list.set_model(Gtk.NoSelection(model=self.fav_store))
        self.fav_list.set_factory(self._factory(self._bind_fav_row))
        self.fav_list.connect(
            "activate", lambda _lv, pos: self._play_from(self._visible_favs, pos)
        )

        self.playlist_store = Gio.ListStore(item_type=Playlist)
        self.playlist_grid.set_model(Gtk.SingleSelection(model=self.playlist_store))
        self.playlist_grid.set_factory(self._factory(self._bind_playlist_card))
        self.playlist_grid.set_single_click_activate(True)
        self.playlist_grid.connect(
            "activate", lambda g, p: self._open_playlist(g.get_model().get_item(p).id)
        )

    def _factory(self, bind_fn):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, item: item.set_child(Gtk.Box()))
        factory.connect("bind", lambda _f, item: bind_fn(item))
        return factory

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

        # Three-dot menu button: hidden until the card is hovered, so it can't
        # be clicked before appearing and it only steals text width on hover.
        menu_btn = Gtk.Button(icon_name="lyre-more-symbolic", valign=Gtk.Align.CENTER,
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

    def _bind_artist_card(self, item):
        artist = item.get_item()
        box = item.get_child()
        if not hasattr(box, "swatch"):
            box = self._card_widget()
            item.set_child(box)
        box.swatch.set_placeholder("artist photo")
        box.swatch.set_path(artist.photo_path or None)
        box.title.set_label(artist.name)
        box.subtitle.set_label(f"{artist.album_count} albums · {artist.track_count} tracks")
        self._attach_menu(box, "artist", artist.id, ARTIST_ENTRIES)

    def _bind_album_card(self, item):
        album = item.get_item()
        box = item.get_child()
        if not hasattr(box, "swatch"):
            box = self._card_widget()
            item.set_child(box)
        box.swatch.set_placeholder("cover art")
        box.swatch.set_path(album.cover_path or None)
        box.title.set_label(album.title)
        box.subtitle.set_label(album.artist)
        self._attach_menu(box, "album", album.id, ALBUM_ENTRIES)

    def _bind_playlist_card(self, item):
        playlist = item.get_item()
        box = item.get_child()
        if not hasattr(box, "swatch"):
            box = self._card_widget()
            item.set_child(box)
        box.swatch.set_placeholder("playlist")
        box.swatch.set_path(playlist.cover_path or None)
        box.title.set_label(playlist.name)
        box.subtitle.set_label(f"{playlist.track_count} tracks")
        self._attach_menu(box, "playlist", playlist.id, PLAYLIST_ENTRIES)

    def _track_row_widget(self):
        row = Gtk.Box(spacing=14, margin_top=6, margin_bottom=6, margin_start=4, margin_end=4)
        row.add_css_class("track-row")
        index_lbl = Gtk.Label(width_chars=2, xalign=0, css_classes=["track-index"])
        title_lbl = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                               css_classes=["track-title"])
        sub_lbl = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["mono-dim-sm"])
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        text_box.append(title_lbl)
        text_box.append(sub_lbl)
        album_lbl = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["mono-dim"],
                               width_chars=14)
        duration_lbl = Gtk.Label(css_classes=["mono-dim"])
        heart_btn = Gtk.Button(icon_name="non-starred-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text="Favourite",
                                css_classes=["flat", "heart-btn"])
        heart_btn.connect("clicked", lambda _b, r=row: self._on_heart_clicked(r))
        row.append(index_lbl)
        row.append(text_box)
        row.append(album_lbl)
        row.append(duration_lbl)
        row.append(heart_btn)
        row.set_cursor(POINTER_CURSOR)
        heart_btn.set_cursor(POINTER_CURSOR)
        row.index_lbl, row.title_lbl, row.sub_lbl = index_lbl, title_lbl, sub_lbl
        row.album_lbl, row.duration_lbl, row.heart_btn = album_lbl, duration_lbl, heart_btn
        row._track_id = None
        return row

    def _on_heart_clicked(self, row):
        if row._track_id is None:
            return
        track = lib.get_track(self.con, row._track_id)
        if track:
            lib.set_favorite(self.con, row._track_id, not track["favorite"])
            self._reload_all()

    def _fill_track_row(self, row, *, title, sub, album_text, duration, index, playing,
                        track_id=None, fav=False):
        row.index_lbl.set_label("♪" if playing else f"{index + 1:02d}")
        row.title_lbl.set_label(title)
        row.sub_lbl.set_label(sub)
        row.duration_lbl.set_label(_fmt_time(duration))
        row._track_id = track_id
        row.heart_btn.set_icon_name("starred-symbolic" if fav else "non-starred-symbolic")
        if fav:
            row.heart_btn.add_css_class("faved")
        else:
            row.heart_btn.remove_css_class("faved")
        if album_text is None:
            row.album_lbl.set_visible(False)
        else:
            row.album_lbl.set_visible(True)
            row.album_lbl.set_label(album_text)
        if playing:
            row.index_lbl.add_css_class("playing")
            row.title_lbl.add_css_class("playing")
        else:
            row.index_lbl.remove_css_class("playing")
            row.title_lbl.remove_css_class("playing")

    def _is_playing_track(self, track_id):
        return (self.queue.current is not None
                and track_id == self.queue.current.id
                and self.player.is_playing())

    def _bind_track_row(self, item):
        self._bind_track_row_from(item, self._visible_tracks)

    def _bind_fav_row(self, item):
        self._bind_track_row_from(item, self._visible_favs)

    def _bind_track_row_from(self, item, tracks):
        t = item.get_item()
        row = item.get_child()
        if not hasattr(row, "title_lbl"):
            row = self._track_row_widget()
            item.set_child(row)
        index = tracks.index(t) if t in tracks else 0
        self._fill_track_row(row, title=t.title, sub=t.artist, album_text=t.album,
                              duration=t.duration, index=index, playing=self._is_playing_track(t.id),
                              track_id=t.id, fav=t.favorite)
        self._attach_menu(row, "track", t.id, TRACK_ENTRIES)

    # ---------- context menus ----------

    def _attach_menu(self, widget, kind, item_id, entries, extra=None):
        # Rows/cards get recycled by GridView/ListView, so bind() may be called
        # many times on the same widget: only attach the gesture once, but keep
        # its target (kind/id/entries) fresh via attributes read at click-time.
        widget._menu_kind = kind
        widget._menu_item_id = item_id
        widget._menu_entries = entries
        widget._menu_extra = extra or {}
        if getattr(widget, "_lyre_menu_attached", False):
            return
        widget._lyre_menu_attached = True
        gesture = Gtk.GestureClick(button=3)
        gesture.connect("pressed",
                        lambda _g, _n, x, y: self._show_item_menu(widget, widget, x, y))
        widget.add_controller(gesture)

    def _build_item_menu(self, widget):
        """Build the context Gio.Menu from widget._menu_* attributes."""
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
            if action == "__playlists__":
                sub = Gio.Menu()
                for pl in lib.all_playlists(self.con):
                    mi = Gio.MenuItem.new(pl["name"], None)
                    mi.set_action_and_target_value("item.add-to-playlist", payload(pl=pl["id"]))
                    sub.append_item(mi)
                mi = Gio.MenuItem.new("New Playlist…", None)
                mi.set_action_and_target_value("item.add-to-new-playlist", payload())
                sub.append_item(mi)
                section.append_submenu(label, sub)
                continue
            if action == "toggle-fav":
                row = lib.get_track(self.con, widget._menu_item_id)
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
        # Unparent only AFTER the menu action has dispatched: GTK closes the
        # popover first and resolves the clicked item's action afterwards, so
        # unparenting directly in "closed" cuts the popover off from the
        # window's action groups and the click silently does nothing.
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()
        return popover

    def _lookup_related(self, kind, item_id, field):
        if kind == "track":
            row = self.con.execute("SELECT artist_id, album_id FROM tracks WHERE id=?", (item_id,)).fetchone()
        else:
            row = self.con.execute("SELECT artist_id FROM albums WHERE id=?", (item_id,)).fetchone()
        return row[field] if row else None

    def _resolve_tracks(self, kind, item_id):
        if kind == "track":
            rows = [lib.get_track(self.con, item_id)]
        elif kind == "album":
            rows = lib.tracks_by_album(self.con, item_id)
        elif kind == "playlist":
            rows = lib.playlist_tracks(self.con, item_id)
        else:
            rows = lib.tracks_by_artist(self.con, item_id)
        return [
            Track(id=r["id"], path=r["path"], title=r["title"], artist=r["artist_name"],
                  album=r["album_title"], album_id=r["album_id"], track_no=r["track_no"] or 0,
                  duration=r["duration"] or 0.0)
            for r in rows if r
        ]

    def _on_item_action(self, action, param):
        data = json.loads(param.get_string())
        kind, item_id, name = data["kind"], data["id"], action.get_name()

        if name == "delete":
            self._confirm_delete(kind, item_id)
            return
        if name == "show-artist":
            artist_id = item_id if kind == "artist" else self._lookup_related(kind, item_id, "artist_id")
            if artist_id:
                self._select_tab("artists")
                self._open_artist(artist_id)
            return
        if name == "show-album":
            album_id = item_id if kind == "album" else self._lookup_related(kind, item_id, "album_id")
            if album_id:
                self._select_tab("albums")
                self._open_album(album_id)
            return
        if name == "set-image":
            self._pick_image(kind, item_id)
            return
        if name == "toggle-fav":
            row = lib.get_track(self.con, item_id)
            if row:
                lib.set_favorite(self.con, item_id, not row["favorite"])
                self._reload_all()
            return
        if name == "add-to-playlist":
            lib.add_to_playlist(self.con, data["pl"], [item_id])
            playlist = lib.get_playlist(self.con, data["pl"])
            self._toast(f'Added to "{playlist["name"]}"' if playlist else "Added to playlist")
            self._reload_all()
            return
        if name == "add-to-new-playlist":
            self._prompt_name(
                "New Playlist", "",
                lambda text: (lib.add_to_playlist(self.con, lib.create_playlist(self.con, text),
                                                  [item_id]),
                              self._toast(f'Added to "{text}"'),
                              self._reload_all()),
            )
            return
        if name == "remove-from-playlist":
            lib.remove_from_playlist(self.con, data["pl"], item_id)
            self._reload_all()
            return
        if name == "edit-meta":
            self._edit_metadata(item_id)
            return
        if name == "edit-album":
            self._edit_album(item_id)
            return
        if name == "rename-artist":
            self._rename_artist(item_id)
            return
        if name == "rename-playlist":
            playlist = lib.get_playlist(self.con, item_id)
            if playlist:
                self._prompt_name(
                    "Rename Playlist", playlist["name"],
                    lambda text: (lib.rename_playlist(self.con, item_id, text),
                                  self._reload_all()),
                )
            return

        tracks = self._resolve_tracks(kind, item_id)
        if name == "play":
            self.queue.play(tracks)
            self._start_current()
        elif name == "play-next":
            self.queue.play_next(tracks)
        elif name == "play-last":
            self.queue.play_last(tracks)
        self._refresh_upnext()

    def _pick_image(self, kind, item_id):
        """Let the user pick a local image as the artist photo / album cover."""
        image_filter = Gtk.FileFilter()
        image_filter.set_name("Images")
        image_filter.add_mime_type("image/png")
        image_filter.add_mime_type("image/jpeg")
        image_filter.add_mime_type("image/webp")
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        filters.append(image_filter)
        dialog = Gtk.FileDialog(default_filter=image_filter, filters=filters)
        dialog.open(self, None, lambda d, res: self._image_chosen(d, res, kind, item_id))

    def _image_chosen(self, dialog, result, kind, item_id):
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        if not gfile:
            return
        src = gfile.get_path()
        if not src:
            return
        ext = Path(src).suffix.lower() or ".png"
        if kind == "artist":
            dest = lib.PHOTOS_DIR / f"custom-{item_id}{ext}"
            shutil.copyfile(src, dest)
            lib.set_artist_photo(self.con, item_id, str(dest))
        else:
            dest = lib.COVERS_DIR / f"custom-{item_id}{ext}"
            shutil.copyfile(src, dest)
            lib.set_album_cover(self.con, item_id, str(dest))
        self._reload_all()
        if self.queue.current and self._player_art is not None:
            album = lib.get_album(self.con, self.queue.current.album_id)
            self._player_art.set_path((album["cover_path"] if album else None) or None)

    def _confirm_delete(self, kind, item_id):
        if kind == "playlist":
            heading = "Delete playlist?"
            body = "This deletes the playlist. Tracks stay in your library."
        else:
            heading = "Remove from library?"
            body = "This only removes it from your library. Files on disk are not touched."
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda d, r: self._do_delete(kind, item_id) if r == "remove" else None)
        dialog.present(self)

    def _do_delete(self, kind, item_id):
        {"track": lib.delete_track, "album": lib.delete_album, "artist": lib.delete_artist,
         "playlist": lib.delete_playlist}[kind](self.con, item_id)
        if self.view == "detail" and (
            (kind == "artist" and self._detail_mode == "artist" and self._detail_artist_id == item_id)
            or (kind == "album" and self._detail_mode == "album" and self._detail_album_id == item_id)
            or (kind == "playlist" and self._detail_mode == "playlist" and self._detail_playlist_id == item_id)
        ):
            self._go_back()
        self._reload_all()

    def _prompt_name(self, heading, initial, on_accept):
        """Small name-entry dialog used for creating/renaming playlists."""
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

    def _edit_metadata(self, track_id):
        """Edit a track's tags: written to the file itself, then rescanned."""
        row = lib.get_track(self.con, track_id)
        if not row:
            return
        fields = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                             css_classes=["boxed-list"], margin_top=8)
        title_row = Adw.EntryRow(title="Title", text=row["title"] or "")
        artist_row = Adw.EntryRow(title="Artist", text=row["artist_name"] or "")
        album_row = Adw.EntryRow(title="Album", text=row["album_title"] or "")
        no_row = Adw.EntryRow(title="Track number",
                              text=str(row["track_no"] or ""))
        for r in (title_row, artist_row, album_row, no_row):
            fields.append(r)

        dialog = Adw.AlertDialog(heading="Edit Metadata",
                                 body=Path(row["path"]).name,
                                 extra_child=fields)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def on_response(_d, response):
            if response != "save":
                return
            try:
                track_no = int(no_row.get_text().strip() or 0)
            except ValueError:
                track_no = 0
            try:
                lib.write_tags(row["path"],
                               title=title_row.get_text().strip() or row["title"],
                               artist=artist_row.get_text().strip() or row["artist_name"],
                               album=album_row.get_text().strip() or row["album_title"],
                               track_no=track_no)
            except Exception as e:
                self._toast(f"Couldn't write tags: {e}")
                return
            lib.scan_file(self.con, row["path"])
            lib.prune_orphans(self.con)
            self._reload_all()
            self._toast("Metadata saved")

        dialog.connect("response", on_response)
        dialog.present(self)

    def _edit_album(self, album_id):
        """Rename an album / set its year: written into every file's tags."""
        album = lib.get_album(self.con, album_id)
        if not album:
            return
        fields = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                             css_classes=["boxed-list"], margin_top=8)
        title_row = Adw.EntryRow(title="Title", text=album["title"] or "")
        year_row = Adw.EntryRow(title="Year", text=str(album["year"] or ""))
        fields.append(title_row)
        fields.append(year_row)

        dialog = Adw.AlertDialog(heading="Edit Album",
                                 body=f"Tags are updated in every file of “{album['title']}”.",
                                 extra_child=fields)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def on_response(_d, response):
            if response != "save":
                return
            title = title_row.get_text().strip() or album["title"]
            try:
                year = int(year_row.get_text().strip() or 0) or None
            except ValueError:
                year = None
            self._toast("Updating album…")

            def work():
                failed = lib.retag_album(self.con, album_id, title=title, year=year)
                GLib.idle_add(self._reload_all)
                message = ("Album updated" if not failed
                           else f"Couldn't write {len(failed)} file(s)")
                GLib.idle_add(lambda: self._toast(message) and False)

            threading.Thread(target=work, daemon=True).start()

        dialog.connect("response", on_response)
        dialog.present(self)

    def _rename_artist(self, artist_id):
        artist = lib.get_artist(self.con, artist_id)
        if not artist:
            return

        def on_accept(text):
            self._toast("Renaming artist…")

            def work():
                failed = lib.rename_artist(self.con, artist_id, text)
                GLib.idle_add(self._reload_all)
                message = ("Artist renamed" if not failed
                           else f"Couldn't write {len(failed)} file(s)")
                GLib.idle_add(lambda: self._toast(message) and False)

            threading.Thread(target=work, daemon=True).start()

        self._prompt_name("Rename Artist", artist["name"], on_accept)

    # ---------- sleep timer ----------

    def _on_sleep_timer(self, action, param):
        minutes = param.get_int32()
        action.set_state(param)
        if self._sleep_source:
            GLib.source_remove(self._sleep_source)
            self._sleep_source = None
        if minutes > 0:
            self._sleep_source = GLib.timeout_add_seconds(
                minutes * 60, self._sleep_timer_fire)
            self._toast(f"Sleep timer — pausing in {minutes} minutes")
        else:
            self._toast("Sleep timer off")

    def _sleep_timer_fire(self):
        self._sleep_source = None
        action = self.lookup_action("sleep-timer")
        if action:
            action.set_state(GLib.Variant("i", 0))
        if self.player.is_playing():
            self._toggle_play()
            self._toast("Sleep timer — playback paused")
        return False

    def _on_new_playlist(self):
        self._prompt_name(
            "New Playlist", "",
            lambda text: (lib.create_playlist(self.con, text),
                          self._select_tab("playlists"),
                          self._reload_all()),
        )

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
            title="Music Folders",
            description="Folders Lyre scans for music",
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
        add_row = Adw.ActionRow(title="Add Music Folder…", activatable=True)
        add_row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        add_row.connect("activated", lambda *_: (dialog.close(), self._on_add_folder()))
        folders.add(add_row)
        watch_row = Adw.SwitchRow(
            title="Watch music folders",
            subtitle="Rescan automatically when files in your music folders change",
        )
        self.settings.bind("watch-folders", watch_row, "active",
                           Gio.SettingsBindFlags.DEFAULT)
        folders.add(watch_row)
        page.add(folders)

        playback = Adw.PreferencesGroup(title="Playback")
        notify_row = Adw.SwitchRow(
            title="Track change notifications",
            subtitle="Show a notification when the track changes and Lyre is in the background",
        )
        self.settings.bind("notify-on-track-change", notify_row, "active",
                           Gio.SettingsBindFlags.DEFAULT)
        playback.add(notify_row)
        page.add(playback)

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
            body=f"Tracks from “{path}” will be removed from your library. "
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
            body="All artists, albums, tracks, favourites, playlists and play "
                 "history will be erased. Your music files on disk are not touched.",
        )
        confirm.add_response("cancel", "Cancel")
        confirm.add_response("delete", "Delete Library")
        confirm.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, response):
            if response != "delete":
                return
            lib.wipe_library(self.con)
            self.queue.current = None
            self.queue.upcoming.clear()
            self.queue.history.clear()
            self.queue.invalidate_peek()
            self.player.stop()
            self.play_icon.set_from_icon_name("lyre-play-symbolic")
            self._update_inhibit(False)
            self._set_player_revealed(False)
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
            GLib.idle_add(self._toast_track_count)

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
        # An empty library shows the "No Music Yet" page instead of blank grids.
        if not self._tracks_all and name in ("artists", "albums", "tracks", "favourites"):
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

    def _open_artist(self, artist_id, select_album_id=None):
        artist = lib.get_artist(self.con, artist_id)
        if not artist:
            return
        self.view = "detail"
        self._detail_mode = "artist"
        self._detail_artist_id = artist_id
        self._detail_album_filter = select_album_id
        self.paper_stack.set_visible_child_name("detail")
        self.detail_back_row.set_visible(True)
        self.sort_btn.set_visible(False)
        self._render_detail()

    def _open_album(self, album_id):
        album = lib.get_album(self.con, album_id)
        if not album:
            return
        self.view = "detail"
        self._detail_mode = "album"
        self._detail_album_id = album_id
        self.paper_stack.set_visible_child_name("detail")
        self.detail_back_row.set_visible(True)
        self.sort_btn.set_visible(False)
        self._render_detail()

    def _open_playlist(self, playlist_id):
        playlist = lib.get_playlist(self.con, playlist_id)
        if not playlist:
            return
        self.view = "detail"
        self._detail_mode = "playlist"
        self._detail_playlist_id = playlist_id
        self.paper_stack.set_visible_child_name("detail")
        self.detail_back_row.set_visible(True)
        self.sort_btn.set_visible(False)
        self._render_detail()

    def _go_back(self):
        self._select_tab(self._last_tab if self._last_tab in VIEW_NAMES else "artists")

    def _clear_box(self, box):
        child = box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    def _render_detail(self):
        if self._detail_mode == "album":
            self._render_album_detail()
        elif self._detail_mode == "playlist":
            self._render_playlist_detail()
        else:
            self._render_artist_detail()

    def _set_detail_tracks(self, rows, *, sub_field, entries=TRACK_ENTRIES, extra=None,
                           reorderable=False):
        """Fill the detail track list from library rows; sub_field picks the
        secondary line ("album_title" on artist pages, "artist_name" on album
        and playlist pages). With reorderable=True rows can be drag-reordered
        (playlist pages)."""
        self._clear_box(self.detail_tracks_box)
        self._detail_tracks = [
            Track(id=r["id"], path=r["path"], title=r["title"], artist=r["artist_name"],
                  album=r["album_title"], album_id=r["album_id"], track_no=r["track_no"] or 0,
                  duration=r["duration"] or 0.0)
            for r in rows
        ]
        for i, (t, r) in enumerate(zip(self._detail_tracks, rows)):
            row = self._track_row_widget()
            self._fill_track_row(row, title=t.title, sub=r[sub_field], album_text=None,
                                  duration=t.duration, index=i, playing=self._is_playing_track(t.id),
                                  track_id=t.id, fav=bool(r["favorite"]))
            gesture = Gtk.GestureClick(button=1)
            gesture.connect("released", lambda _g, _n, _x, _y, pos=i: self._play_from(self._detail_tracks, pos))
            row.add_controller(gesture)
            if reorderable:
                drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
                drag.connect(
                    "prepare",
                    lambda _s, _x, _y, pos=i: Gdk.ContentProvider.new_for_value(str(pos)),
                )
                row.add_controller(drag)
                drop = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
                drop.connect(
                    "drop",
                    lambda _t, value, _x, _y, pos=i: self._on_reorder_drop(value, pos),
                )
                row.add_controller(drop)
            self._attach_menu(row, "track", t.id, entries, extra=extra)
            self.detail_tracks_box.append(row)

    def _on_reorder_drop(self, value, dst):
        try:
            src = int(value)
        except (TypeError, ValueError):
            return False
        order = self._detail_entry_ids
        if src == dst or not (0 <= src < len(order)) or not (0 <= dst < len(order)):
            return False
        entry = order.pop(src)
        order.insert(dst, entry)
        lib.reorder_playlist(self.con, self._detail_playlist_id, order)
        self._reload_all()
        return True

    def _render_playlist_detail(self):
        playlist = lib.get_playlist(self.con, self._detail_playlist_id)
        if not playlist:
            self._go_back()
            return

        self.detail_kind_label.set_label("Playlist")
        self.detail_albums_section.set_visible(False)

        tracks = lib.playlist_tracks(self.con, self._detail_playlist_id)

        self._clear_box(self.detail_hero_slot)
        hero = Swatch("playlist", size=108)
        hero.set_path(next((r["cover_path"] for r in tracks if r["cover_path"]), None))
        self.detail_hero_slot.append(hero)
        self.detail_name_label.set_label(playlist["name"])

        total = sum(r["duration"] or 0 for r in tracks)
        self.detail_stats_label.set_label(f"{len(tracks)} tracks · {_fmt_time(total)}")
        self.detail_filter_label.set_label("")

        self._detail_entry_ids = [r["entry_id"] for r in tracks]
        self._set_detail_tracks(tracks, sub_field="artist_name",
                                 entries=PLAYLIST_TRACK_ENTRIES,
                                 extra={"pl": self._detail_playlist_id},
                                 reorderable=True)

    def _render_artist_detail(self):
        artist = lib.get_artist(self.con, self._detail_artist_id)
        if not artist:
            self._go_back()
            return

        self.detail_kind_label.set_label("Artist")
        self.detail_albums_section.set_visible(True)

        self._clear_box(self.detail_hero_slot)
        hero = Swatch("artist photo", size=108)
        hero.set_path(artist["photo_path"] or None)
        self.detail_hero_slot.append(hero)
        self.detail_name_label.set_label(artist["name"])

        albums = lib.albums_by_artist(self.con, artist["id"])
        artist_tracks = lib.tracks_by_artist(self.con, artist["id"])
        self.detail_stats_label.set_label(f"{len(albums)} albums · {len(artist_tracks)} tracks")

        self._clear_box(self.detail_albums_box)
        self._detail_album_ids = [a["id"] for a in albums]
        for a in albums:
            chip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, width_request=84)
            swatch = Swatch(None, size=84)
            swatch.set_path(a["cover_path"] or None)
            if self._detail_album_filter == a["id"]:
                swatch.add_css_class("chip-selected")
            # max_width_chars caps the label's *natural* width so a long album
            # title can't widen its chip past the 84px swatch (the flow box
            # gives each chip its natural width, so one long title made the
            # first chip wider than the rest).
            label = Gtk.Label(label=a["title"], justify=Gtk.Justification.CENTER,
                               ellipsize=Pango.EllipsizeMode.END,
                               max_width_chars=8, width_chars=0,
                               css_classes=["chip-label"])
            chip.set_cursor(POINTER_CURSOR)
            chip.append(swatch)
            chip.append(label)
            self._attach_menu(chip, "album", a["id"], ALBUM_ENTRIES)
            self.detail_albums_box.append(chip)

        if not self._flowbox_connected:
            self.detail_albums_box.connect("child-activated", self._on_album_chip_activated)
            self._flowbox_connected = True

        if self._detail_album_filter:
            filtered = lib.tracks_by_album(self.con, self._detail_album_filter)
            album = lib.get_album(self.con, self._detail_album_filter)
            self.detail_filter_label.set_label(album["title"] if album else "All")
        else:
            filtered = artist_tracks
            self.detail_filter_label.set_label("All")

        self._set_detail_tracks(filtered, sub_field="album_title")

    def _render_album_detail(self):
        album = lib.get_album(self.con, self._detail_album_id)
        if not album:
            self._go_back()
            return

        self.detail_kind_label.set_label("Album")
        self.detail_albums_section.set_visible(False)

        self._clear_box(self.detail_hero_slot)
        hero = Swatch("cover art", size=108)
        hero.set_path(album["cover_path"] or None)
        self.detail_hero_slot.append(hero)
        self.detail_name_label.set_label(album["title"])

        tracks = lib.tracks_by_album(self.con, album["id"])
        parts = [album["artist_name"]]
        if album["year"]:
            parts.append(str(album["year"]))
        parts.append(f"{len(tracks)} tracks")
        self.detail_stats_label.set_label(" · ".join(parts))
        self.detail_filter_label.set_label("")

        self._set_detail_tracks(tracks, sub_field="artist_name")

    def _on_album_chip_activated(self, _flowbox, child):
        idx = child.get_index()
        if 0 <= idx < len(self._detail_album_ids):
            album_id = self._detail_album_ids[idx]
            self._detail_album_filter = None if self._detail_album_filter == album_id else album_id
            self._render_detail()

    # ---------- search ----------

    def _on_search_changed(self, entry):
        self._search_query = entry.get_text().strip().lower()
        self._apply_filters()

    def _sorted_artists(self, artists):
        if self._sort["artists"] == "plays":
            return sorted(artists, key=lambda a: (-self._artist_plays.get(a.id, 0),
                                                  a.name.lower()))
        return artists  # library order is already by name

    def _sorted_albums(self, albums):
        mode = self._sort["albums"]
        if mode == "title":
            return sorted(albums, key=lambda a: a.title.lower())
        if mode == "year":
            return sorted(albums, key=lambda a: (-(a.year or 0), a.artist.lower()))
        if mode == "plays":
            return sorted(albums, key=lambda a: (-self._album_plays.get(a.id, 0),
                                                 a.title.lower()))
        return albums  # library order is already artist, year

    def _sorted_tracks(self, tracks):
        mode = self._sort["tracks"]
        if mode == "artist":
            return sorted(tracks, key=lambda t: (t.artist.lower(), t.album.lower(),
                                                 t.track_no))
        if mode == "album":
            return sorted(tracks, key=lambda t: (t.album.lower(), t.track_no))
        if mode == "plays":
            return sorted(tracks, key=lambda t: (-self._track_plays.get(t.id, 0),
                                                 t.title.lower()))
        if mode == "recent":
            return sorted(tracks, key=lambda t: -t.id)
        return tracks  # library order is already by title

    def _apply_filters(self):
        q = self._search_query
        self.artist_store.remove_all()
        for a in self._sorted_artists(self._artists_all):
            if not q or q in a.name.lower():
                self.artist_store.append(a)

        self.album_store.remove_all()
        for a in self._sorted_albums(self._albums_all):
            if not q or q in a.title.lower() or q in a.artist.lower():
                self.album_store.append(a)

        self.track_store.remove_all()
        self._visible_tracks = [
            t for t in self._sorted_tracks(self._tracks_all)
            if not q or q in t.title.lower() or q in t.artist.lower() or q in t.album.lower()
        ]
        for t in self._visible_tracks:
            self.track_store.append(t)

        self.fav_store.remove_all()
        self._visible_favs = [t for t in self._visible_tracks if t.favorite]
        for t in self._visible_favs:
            self.fav_store.append(t)

        self.playlist_store.remove_all()
        for p in self._playlists_all:
            if not q or q in p.name.lower():
                self.playlist_store.append(p)

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
            GLib.idle_add(self._toast_track_count)

        threading.Thread(target=work, daemon=True).start()

    def _toast_track_count(self):
        self._toast(f"Library updated — {len(self._tracks_all)} tracks")
        return False

    def _on_fetch_metadata(self):
        """Fetch missing cover art / artist photos from MusicBrainz + Wikidata."""
        self._toast("Fetching covers and artist photos…")

        def work():
            meta.fetch_all_missing(self.con)
            GLib.idle_add(self._reload_all)
            GLib.idle_add(lambda: self._toast("Metadata fetch finished") and False)

        threading.Thread(target=work, daemon=True).start()

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
            GLib.idle_add(self._toast_track_count)
            meta.fetch_all_missing(self.con)
            GLib.idle_add(self._reload_all)

        threading.Thread(target=work, daemon=True).start()

    def _reload_all(self):
        self._artists_all = [
            Artist(id=r["id"], name=r["name"], photo_path=r["photo_path"] or "",
                   album_count=r["album_count"] or 0, track_count=r["track_count"] or 0)
            for r in lib.all_artists(self.con)
        ]
        self._albums_all = [
            Album(id=r["id"], title=r["title"], artist=r["artist_name"],
                  year=r["year"] or 0, cover_path=r["cover_path"] or "")
            for r in lib.all_albums(self.con)
        ]
        self._tracks_all = [
            Track(id=r["id"], path=r["path"], title=r["title"], artist=r["artist_name"],
                  album=r["album_title"], album_id=r["album_id"],
                  track_no=r["track_no"] or 0, duration=r["duration"] or 0.0,
                  favorite=bool(r["favorite"]))
            for r in lib.all_tracks(self.con)
        ]
        self._playlists_all = [
            Playlist(id=r["id"], name=r["name"], track_count=r["track_count"] or 0,
                     cover_path=r["cover_path"] or "")
            for r in lib.all_playlists(self.con)
        ]
        self._track_plays = dict(self.con.execute(
            "SELECT track_id, COUNT(*) FROM plays GROUP BY track_id").fetchall())
        self._artist_plays = dict(self.con.execute(
            """SELECT t.artist_id, COUNT(*) FROM plays p
               JOIN tracks t ON t.id = p.track_id GROUP BY t.artist_id""").fetchall())
        self._album_plays = dict(self.con.execute(
            """SELECT t.album_id, COUNT(*) FROM plays p
               JOIN tracks t ON t.id = p.track_id GROUP BY t.album_id""").fetchall())
        self._prune_queue()
        self._apply_filters()
        if self.view == "detail" and (self._detail_artist_id is not None
                                       or self._detail_album_id is not None
                                       or self._detail_playlist_id is not None):
            self._render_detail()
        elif self.view in VIEW_NAMES:
            self._select_tab(self.view)  # refreshes the empty-state page
        return False

    def _prune_queue(self):
        """Sync the queue with a reloaded library: drop tracks that no longer
        exist (files deleted, folder removed…) and refresh the rest so tag
        edits show up. If the playing track itself is gone, stop playback and
        hide the player."""
        by_id = {t.id: t for t in self._tracks_all}
        q = self.queue
        q.invalidate_peek()
        q.upcoming = [by_id[t.id] for t in q.upcoming if t.id in by_id]
        q.history = [by_id[t.id] for t in q.history if t.id in by_id]
        if q.current is not None:
            fresh = by_id.get(q.current.id)
            if fresh is None:
                q.current = None
                self.player.stop()
                self.play_icon.set_from_icon_name("lyre-play-symbolic")
                self._update_inhibit(False)
                self._set_player_revealed(False)
            else:
                q.current = fresh
                self.now_title.set_label(fresh.title)
                self.now_artist.set_label(fresh.artist)
        self._refresh_upnext()
        if getattr(self, "mpris", None):
            self.mpris.update()

    # ---------- playback ----------

    def _setup_player_controls(self):
        self.play_btn.connect("clicked", lambda *_: self._toggle_play())
        self.prev_btn.connect("clicked", lambda *_: self._on_prev())
        self.next_btn.connect("clicked", lambda *_: self._advance())
        self.shuffle_btn.connect("toggled", lambda b: setattr(self.queue, "shuffle", b.get_active()))
        self.repeat_btn.connect("toggled", lambda b: setattr(self.queue, "repeat", b.get_active()))
        self.upnext_clear_btn.connect("clicked", lambda *_: self._clear_upnext())
        self.seek_scale.connect("change-value", self._on_seek)
        self.volume_scale.connect("value-changed", self._on_volume_changed)
        self.player.set_volume(self.volume_scale.get_value())

    def _clear_upnext(self):
        self.queue.upcoming.clear()
        self.queue.invalidate_peek()
        self._refresh_upnext()

    def _on_track_activate(self, _list_view, position):
        self._play_from(self._visible_tracks, position)

    def _play_from(self, tracks, position):
        self.queue.play(list(tracks[position:]))
        self._start_current()

    def _start_current(self):
        t = self.queue.current
        if not t:
            return
        self._gapless_pending = None
        self.player.load(t.path)
        self.player.play()
        self._track_started(t)

    def _track_started(self, t):
        """UI + bookkeeping for a track that just began playing, whether from
        a manual start or a gapless hand-over."""
        self.now_title.set_label(t.title)
        self.now_artist.set_label(t.artist)

        # One persistent swatch, reused across track changes.
        if self._player_art is None:
            self._player_art = Swatch("cover art", size=self.PLAYER_WIDTH)
            self._player_art.set_hexpand(True)
            self.player_art_slot.append(self._player_art)
        album = lib.get_album(self.con, t.album_id) if t.album_id else None
        self._player_art.set_path((album["cover_path"] if album else None) or None)

        self._set_player_revealed(True)
        self.play_icon.set_from_icon_name("lyre-pause-symbolic")
        self._refresh_upnext()
        self._apply_filters()
        lib.record_play(self.con, t.id)
        self._update_inhibit(True)
        self._notify_track(t)
        if getattr(self, "mpris", None):
            self.mpris.update()
        if self.view == "detail":
            self._render_detail()

    # ---------- gapless hand-over ----------

    def _gapless_next_path(self):
        """Called on GStreamer's streaming thread just before the current
        track ends: pick the next track so playback continues seamlessly.
        Only the choice happens here; queue/UI commit on stream start."""
        nxt = self.queue.peek_next()
        if not nxt:
            return None
        self._gapless_pending = nxt
        return nxt.path

    def _on_gapless_started(self):
        if self._gapless_pending is None:
            return False  # stream start from a manual load, already handled
        self._gapless_pending = None
        t = self.queue.advance()
        if t:
            self._track_started(t)
        return False

    def _update_inhibit(self, playing):
        """Keep the session awake while music plays."""
        app = self.get_application()
        if app is None:
            return
        if playing and not self._inhibit_cookie:
            self._inhibit_cookie = app.inhibit(
                self, Gtk.ApplicationInhibitFlags.SUSPEND, "Music is playing")
        elif not playing and self._inhibit_cookie:
            app.uninhibit(self._inhibit_cookie)
            self._inhibit_cookie = 0

    def _notify_track(self, t):
        """Desktop notification on track change while the window is unfocused."""
        if self.is_active() or not self.settings.get_boolean("notify-on-track-change"):
            return
        app = self.get_application()
        if app is None:
            return
        notification = Gio.Notification.new(t.title)
        notification.set_body(t.artist)
        cover = self.current_cover_path()
        if cover:
            notification.set_icon(Gio.FileIcon.new(Gio.File.new_for_path(cover)))
        app.send_notification("now-playing", notification)

    def _on_player_error(self, message):
        """A file failed to play (deleted, corrupt, unreadable): say so and
        move on instead of stalling the queue."""
        t = self.queue.current
        self._toast(f"Couldn't play “{t.title}” — skipping" if t else "Playback error")
        self._advance()
        return False

    def _on_prev(self):
        if self.player.position() > 3:
            self.player.seek(0)
            return
        prev = self.queue.previous()
        if prev:
            self._start_current()
        else:
            self.player.seek(0)

    def _advance(self):
        self._gapless_pending = None
        nxt = self.queue.advance()
        if nxt:
            self._start_current()
        else:
            self.player.stop()
            self.play_icon.set_from_icon_name("lyre-play-symbolic")
            self._update_inhibit(False)
            self._refresh_upnext()
            self._apply_filters()
            if getattr(self, "mpris", None):
                self.mpris.update()
        return False

    # ---------- up next ----------

    def _refresh_upnext(self):
        self._clear_box(self.upnext_box)
        upcoming = self.queue.upcoming[:30]
        self.upnext_header.set_visible(bool(upcoming))
        for i, t in enumerate(upcoming):
            row = Gtk.Box(spacing=10)
            row.add_css_class("upnext-row")
            index_lbl = Gtk.Label(label=f"{i + 1:02d}", width_chars=2, xalign=0,
                                   css_classes=["track-index"])
            text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
            # max_width_chars caps the label's *natural* width so a long title
            # can't stretch the fixed-width player panel (see PLAYER_WIDTH).
            title_lbl = Gtk.Label(label=t.title, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                   max_width_chars=18, width_chars=0,
                                   css_classes=["upnext-title"])
            sub_lbl = Gtk.Label(label=t.artist, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                 max_width_chars=18, width_chars=0,
                                 css_classes=["mono-dim-sm"])
            text_box.append(title_lbl)
            text_box.append(sub_lbl)
            duration_lbl = Gtk.Label(label=_fmt_time(t.duration), css_classes=["mono-dim-sm"])
            remove_btn = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER,
                                     tooltip_text="Remove from queue",
                                     css_classes=["flat", "upnext-remove"])
            remove_btn.connect("clicked", lambda _b, pos=i: self._remove_upcoming(pos))
            row.set_cursor(POINTER_CURSOR)
            remove_btn.set_cursor(POINTER_CURSOR)
            row.append(index_lbl)
            row.append(text_box)
            row.append(duration_lbl)
            row.append(remove_btn)
            gesture = Gtk.GestureClick(button=1)
            gesture.connect("released", lambda *_a, pos=i: self._play_upcoming(pos))
            row.add_controller(gesture)
            drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
            drag.connect("prepare",
                         lambda _s, _x, _y, pos=i: Gdk.ContentProvider.new_for_value(f"upnext:{pos}"))
            row.add_controller(drag)
            drop = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
            drop.connect("drop",
                         lambda _t, value, _x, _y, pos=i: self._on_upnext_reorder(value, pos))
            row.add_controller(drop)
            self.upnext_box.append(row)

    def _remove_upcoming(self, idx):
        if 0 <= idx < len(self.queue.upcoming):
            self.queue.upcoming.pop(idx)
            self.queue.invalidate_peek()
            self._refresh_upnext()

    def _on_upnext_reorder(self, value, dst):
        try:
            kind, src = str(value).split(":", 1)
            src = int(src)
        except (TypeError, ValueError):
            return False
        upcoming = self.queue.upcoming
        if kind != "upnext" or src == dst or not (0 <= src < len(upcoming)) \
                or not (0 <= dst < len(upcoming)):
            return False
        upcoming.insert(dst, upcoming.pop(src))
        self.queue.invalidate_peek()
        self._refresh_upnext()
        return True

    def _play_upcoming(self, idx):
        tracks = self.queue.upcoming[idx:]
        if tracks:
            self.queue.play(tracks)
            self._start_current()

    def _on_space_pressed(self, _ctl, keyval, _keycode, state):
        if keyval != Gdk.KEY_space:
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                    | Gdk.ModifierType.SHIFT_MASK):
            return False
        # Never steal space from a text field (the focused widget inside an
        # entry is a Gtk.Text; TextView/Editable cover any future editors).
        focus = self.get_focus()
        if isinstance(focus, (Gtk.Text, Gtk.TextView)) or isinstance(focus, Gtk.Editable):
            return False
        self._toggle_play()
        return True

    def _toggle_play(self):
        if not self.queue.current:
            return
        if self.player.is_playing():
            self.player.pause()
            self.play_icon.set_from_icon_name("lyre-play-symbolic")
            self._update_inhibit(False)
        else:
            self.player.play()
            self.play_icon.set_from_icon_name("lyre-pause-symbolic")
            self._update_inhibit(True)
        self._apply_filters()
        if getattr(self, "mpris", None):
            self.mpris.update()

    def _on_seek(self, _scale, _scroll, value):
        self.player.seek(value)
        if getattr(self, "mpris", None):
            self.mpris.notify_seeked()
        return False

    def _volume_step(self, delta):
        self.volume_scale.set_value(
            max(0.0, min(1.0, self.volume_scale.get_value() + delta)))

    def _toggle_mute(self):
        value = self.volume_scale.get_value()
        if value > 0.001:
            self._pre_mute_volume = value
            self.volume_scale.set_value(0.0)
        else:
            self.volume_scale.set_value(getattr(self, "_pre_mute_volume", 0.7))

    def _on_volume_changed(self, scale):
        value = scale.get_value()
        self.player.set_volume(value)
        if value <= 0.001:
            level = "muted"
        elif value < 0.34:
            level = "low"
        elif value < 0.67:
            level = "medium"
        else:
            level = "high"
        self.volume_btn.set_icon_name(f"lyre-volume-{level}-symbolic")
        self.volume_btn.set_tooltip_text("Unmute" if level == "muted" else "Mute")

    def _tick(self):
        if self.queue.current:
            dur = self.player.duration() or 1
            pos = self.player.position()
            self.seek_scale.set_range(0, dur)
            self.seek_scale.set_value(pos)
            self.elapsed_label.set_label(_fmt_time(pos))
            self.duration_label.set_label(_fmt_time(dur))
        return True
