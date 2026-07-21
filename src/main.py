import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .window import MusicWindow

APP_ID = "io.github.drvonmiau.Lyre"


class MusicPlayerApp(Adw.Application):
    def __init__(self, version=""):
        super().__init__(application_id=APP_ID)
        self.version = version
        self.window = None

        self.create_action("quit", lambda *_a: self.quit(), ["<primary>q"])
        self.create_action("about", lambda *_a: self._show_about())

    def create_action(self, name, callback, shortcuts=None):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f"app.{name}", shortcuts)

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_resource("/io/github/drvonmiau/Lyre/style.css")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon_theme.add_resource_path("/io/github/drvonmiau/Lyre/icons")
        # Inside the Flatpak sandbox the host's icon themes aren't visible, so
        # symbolic lookups (e.g. the window controls) fall back to Adwaita.
        # With host-os access granted, searching the host's icon dirs lets the
        # user's actual system theme resolve.
        for path in ("/run/host/usr/share/icons", "/run/host/share/icons",
                     os.path.expanduser("~/.local/share/icons"),
                     os.path.expanduser("~/.icons")):
            if os.path.isdir(path):
                icon_theme.add_search_path(path)

    def do_activate(self):
        if self.window is None:
            self.window = MusicWindow(application=self)
        self.window.present()

    def _show_about(self):
        about = Adw.AboutDialog(
            application_name="Lyre",
            application_icon=APP_ID,
            version=self.version or "0.1.0",
            developer_name="Daniel",
            license_type=Gtk.License.GPL_3_0,
            website="https://github.com/drvonmiau/lyre",
            issue_url="https://github.com/drvonmiau/lyre/issues",
        )
        about.present(self.window)


def main(version):
    return MusicPlayerApp(version=version).run(sys.argv)


if __name__ == "__main__":
    sys.exit(main(""))
