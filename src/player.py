"""GStreamer playback + a simple play queue."""
import random

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

Gst.init(None)


class Player:
    def __init__(self, on_eos=None, on_error=None, on_stream_start=None):
        self.playbin = Gst.ElementFactory.make("playbin", "player")
        self._on_eos, self._on_error = on_eos, on_error
        self._on_stream_start = on_stream_start
        self._gapless_cb = None
        self._error_reported = False
        self.playbin.connect("about-to-finish", self._on_about_to_finish)
        bus = self.playbin.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_msg)

    def set_gapless_source(self, callback):
        """callback() -> next file path or None; called from GStreamer's
        streaming thread just before the current track ends, so the next one
        starts without a gap."""
        self._gapless_cb = callback

    def _on_about_to_finish(self, playbin):
        if not self._gapless_cb:
            return
        nxt = self._gapless_cb()
        if nxt:
            playbin.set_property("uri", Gst.filename_to_uri(nxt))

    def _on_msg(self, _bus, msg):
        if msg.type == Gst.MessageType.EOS:
            self.playbin.set_state(Gst.State.NULL)
            if self._on_eos:
                GLib.idle_add(self._on_eos)
        elif msg.type == Gst.MessageType.STREAM_START and self._on_stream_start:
            GLib.idle_add(self._on_stream_start)
        elif msg.type == Gst.MessageType.ERROR and self._on_error:
            # A single failed load spews several ERROR messages; forward only
            # the first so the handler doesn't skip more than one track.
            if self._error_reported:
                return
            self._error_reported = True
            err, _debug = msg.parse_error()
            GLib.idle_add(self._on_error, str(err))

    def load(self, path):
        self.playbin.set_state(Gst.State.NULL)
        self._error_reported = False
        self.playbin.set_property("uri", Gst.filename_to_uri(path))

    def play(self):
        self.playbin.set_state(Gst.State.PLAYING)

    def pause(self):
        self.playbin.set_state(Gst.State.PAUSED)

    def stop(self):
        self.playbin.set_state(Gst.State.NULL)

    def set_volume(self, v):
        self.playbin.set_property("volume", max(0.0, min(1.0, v)))

    def seek(self, seconds):
        self.playbin.seek_simple(
            Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, int(seconds * Gst.SECOND)
        )

    def position(self):
        ok, p = self.playbin.query_position(Gst.Format.TIME)
        return p / Gst.SECOND if ok else 0.0

    def duration(self):
        ok, d = self.playbin.query_duration(Gst.Format.TIME)
        return d / Gst.SECOND if ok else 0.0

    def is_playing(self):
        return self.playbin.get_state(0)[1] == Gst.State.PLAYING


class Queue:
    """Holds the current track (a models.Track), what's up next, and a
    back-history so "previous" can return to the actual prior track.
    Supports shuffle (random pick from upcoming) and repeat (replays the
    finished queue when it runs out)."""

    def __init__(self):
        self.current = None
        self.upcoming = []
        self.history = []
        self.shuffle = False
        self.repeat = False
        self._peeked = None

    def play(self, tracks):
        if not tracks:
            return
        self.history = []
        self._peeked = None
        self.current, self.upcoming = tracks[0], list(tracks[1:])

    def play_next(self, tracks):
        self.upcoming[0:0] = tracks
        self._peeked = None

    def play_last(self, tracks):
        self.upcoming.extend(tracks)

    def invalidate_peek(self):
        """Forget a peeked choice after the queue is edited by hand."""
        self._peeked = None

    def peek_next(self):
        """Decide (and remember) which track advance() will return, without
        committing the transition. Used for gapless: the next URI must be
        chosen before the current track has actually finished."""
        if self._peeked is not None:
            return self._peeked
        pool = self.upcoming
        if not pool and self.repeat:
            pool = self.history + ([self.current] if self.current else [])
        if not pool:
            return None
        self._peeked = pool[random.randrange(len(pool))] if self.shuffle else pool[0]
        return self._peeked

    def advance(self):
        peeked, self._peeked = self._peeked, None
        if self.current is not None:
            self.history.append(self.current)
        if not self.upcoming and self.repeat and self.history:
            self.upcoming = list(self.history)
            self.history = []
        if not self.upcoming:
            self.current = None
            return None
        if peeked is not None and peeked in self.upcoming:
            self.upcoming.remove(peeked)
            self.current = peeked
        else:
            idx = random.randrange(len(self.upcoming)) if self.shuffle else 0
            self.current = self.upcoming.pop(idx)
        return self.current

    def previous(self):
        if not self.history:
            return None
        self._peeked = None
        if self.current is not None:
            self.upcoming.insert(0, self.current)
        self.current = self.history.pop()
        return self.current
