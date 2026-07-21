"""MPRIS (org.mpris.MediaPlayer2) D-Bus service: makes media keys work and
shows Lyre in the desktop's sound menu / lock screen with track metadata.
"""
from gi.repository import Gio, GLib

BUS_NAME = "org.mpris.MediaPlayer2.Lyre"
OBJECT_PATH = "/org/mpris/MediaPlayer2"

INTROSPECTION_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek">
      <arg direction="in" name="Offset" type="x"/>
    </method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri">
      <arg direction="in" name="Uri" type="s"/>
    </method>
    <signal name="Seeked">
      <arg name="Position" type="x"/>
    </signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""


class MprisServer:
    """Owns the MPRIS bus name and bridges D-Bus calls to the main window."""

    def __init__(self, window):
        self.window = window
        self._connection = None
        self._node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        try:
            Gio.bus_own_name(
                Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                self._on_bus_acquired, None, None,
            )
        except Exception:
            # No session bus (rare outside a desktop session); MPRIS is a
            # nice-to-have, never a reason to break playback.
            pass

    def _on_bus_acquired(self, connection, _name):
        self._connection = connection
        for interface in self._node.interfaces:
            connection.register_object(
                OBJECT_PATH, interface,
                self._on_method_call, self._on_get_property, self._on_set_property,
            )

    # ---------- inbound ----------

    def _on_method_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        w = self.window
        if method == "Seek":
            (offset,) = params.unpack()
            target = w.player.position() + offset / 1_000_000
            duration = w.player.duration()
            if duration and target >= duration:
                w._advance()  # per MPRIS spec: seeking past the end skips
            else:
                w.player.seek(max(0.0, target))
                self.notify_seeked()
        elif method == "SetPosition":
            trackid, position = params.unpack()
            current = w.queue.current
            if current and trackid == f"/io/github/drvonmiau/Lyre/track/{current.id}":
                w.player.seek(max(0.0, position / 1_000_000))
                self.notify_seeked()
        elif method == "Raise":
            w.present()
        elif method == "Quit":
            app = w.get_application()
            if app:
                app.quit()
        elif method == "PlayPause":
            w._toggle_play()
        elif method == "Play":
            if w.queue.current and not w.player.is_playing():
                w._toggle_play()
        elif method == "Pause":
            if w.player.is_playing():
                w._toggle_play()
        elif method == "Stop":
            w.player.stop()
            self.update()
        elif method == "Next":
            w._advance()
        elif method == "Previous":
            w._on_prev()
        invocation.return_value(None)

    def _on_get_property(self, _conn, _sender, _path, _iface, prop):
        w = self.window
        static = {
            "CanQuit": GLib.Variant("b", True),
            "CanRaise": GLib.Variant("b", True),
            "HasTrackList": GLib.Variant("b", False),
            "Identity": GLib.Variant("s", "Lyre"),
            "DesktopEntry": GLib.Variant("s", "io.github.drvonmiau.Lyre"),
            "SupportedUriSchemes": GLib.Variant("as", []),
            "SupportedMimeTypes": GLib.Variant("as", []),
            "Rate": GLib.Variant("d", 1.0),
            "MinimumRate": GLib.Variant("d", 1.0),
            "MaximumRate": GLib.Variant("d", 1.0),
            "CanGoNext": GLib.Variant("b", True),
            "CanGoPrevious": GLib.Variant("b", True),
            "CanPlay": GLib.Variant("b", True),
            "CanPause": GLib.Variant("b", True),
            "CanSeek": GLib.Variant("b", True),
            "CanControl": GLib.Variant("b", True),
        }
        if prop in static:
            return static[prop]
        if prop == "PlaybackStatus":
            return GLib.Variant("s", self._status())
        if prop == "Metadata":
            return self._metadata()
        if prop == "Volume":
            return GLib.Variant("d", w.volume_scale.get_value())
        if prop == "Position":
            return GLib.Variant("x", int(w.player.position() * 1_000_000))
        return None

    def _on_set_property(self, _conn, _sender, _path, _iface, prop, value):
        if prop == "Volume":
            self.window.volume_scale.set_value(max(0.0, min(1.0, value.get_double())))
            return True
        return prop == "Rate"  # accept and ignore rate changes

    # ---------- outbound ----------

    def _status(self):
        w = self.window
        if not w.queue.current:
            return "Stopped"
        return "Playing" if w.player.is_playing() else "Paused"

    def _metadata(self):
        w = self.window
        track = w.queue.current
        if not track:
            return GLib.Variant("a{sv}", {})
        meta = {
            "mpris:trackid": GLib.Variant(
                "o", f"/io/github/drvonmiau/Lyre/track/{track.id}"),
            "mpris:length": GLib.Variant("x", int((track.duration or 0) * 1_000_000)),
            "xesam:title": GLib.Variant("s", track.title),
            "xesam:artist": GLib.Variant("as", [track.artist]),
            "xesam:album": GLib.Variant("s", track.album),
        }
        cover = w.current_cover_path()
        if cover:
            meta["mpris:artUrl"] = GLib.Variant("s", Gio.File.new_for_path(cover).get_uri())
        return GLib.Variant("a{sv}", meta)

    def notify_seeked(self):
        """Emit the Seeked signal so desktop position sliders stay in sync."""
        if not self._connection:
            return
        try:
            self._connection.emit_signal(
                None, OBJECT_PATH, "org.mpris.MediaPlayer2.Player", "Seeked",
                GLib.Variant("(x)", (int(self.window.player.position() * 1_000_000),)),
            )
        except GLib.Error:
            pass

    def update(self):
        """Push PlaybackStatus + Metadata to listeners (call on track/state change)."""
        if not self._connection:
            return
        changed = GLib.Variant("(sa{sv}as)", (
            "org.mpris.MediaPlayer2.Player",
            {"PlaybackStatus": GLib.Variant("s", self._status()),
             "Metadata": self._metadata()},
            [],
        ))
        try:
            self._connection.emit_signal(
                None, OBJECT_PATH, "org.freedesktop.DBus.Properties",
                "PropertiesChanged", changed,
            )
        except GLib.Error:
            pass
