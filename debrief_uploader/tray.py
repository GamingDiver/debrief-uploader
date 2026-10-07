"""System tray icon and the review window.

Deliberately thin. Every decision lives in engine.py, which the CLI drives just
as well -- so if the tray misbehaves on a particular PC, `run` without --tray
is a complete fallback and `review` resolves anything waiting.

Needs pystray. tkinter ships with Python on Windows.
"""
import os
import subprocess
import sys
import threading
import time
import webbrowser

from . import config, matcher, ui
from .api import ApiError
from .engine import Engine

IDLE = (0x64, 0x7d, 0x8e)
WATCH = (0x0f, 0x6b, 0x70)
BUSY = (0xff, 0xd1, 0x66)
ATTN = (0xa8, 0x41, 0x0e)


def _icon_image(colour):
    """A small mark drawn at runtime -- no asset file to ship or lose."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([4, 4, 60, 60], fill=(11, 22, 32, 255), outline=colour + (255,), width=5)
    d.polygon([(32, 18), (44, 44), (32, 37), (20, 44)], fill=colour + (255,))
    return im


class Tray:
    def __init__(self, settings, store, client, log):
        self.s = settings
        self.store = store
        self.client = client
        self.log = log
        self.eng = Engine(settings, store, client, log)
        self.paused = False
        self.icon = None
        self._stop = threading.Event()

    # ---- counts used by the menu -----------------------------------------
    def _counts(self):
        held = len(self.store.replays([matcher.HELD]))
        queued = len(self.store.replays([matcher.READY, matcher.UPLOADING]))
        waiting = len(self.store.replays([matcher.AWAITING_SHOTS]))
        return held, queued, waiting

    def _title(self):
        if not self.s.get("watching_confirmed"):
            return "Debrief Uploader - check your folders in Settings to start"
        if not self.client.session.signed_in:
            return "Debrief Uploader - not signed in"
        if self.eng.blocked:
            return "Debrief Uploader - %s" % self.eng.blocked
        if self.paused:
            return "Debrief Uploader - paused"
        held, queued, waiting = self._counts()
        bits = []
        if held:
            bits.append("%d need%s you" % (held, "s" if held == 1 else ""))
        if queued:
            bits.append("%d uploading" % queued)
        if waiting:
            bits.append("%d waiting for scorecards" % waiting)
        return "Debrief Uploader - " + (", ".join(bits) or "watching")

    def _colour(self):
        held, queued, _ = self._counts()
        if not self.s.get("watching_confirmed"):
            return ATTN
        if self.eng.blocked or not self.client.session.signed_in or held:
            return ATTN if held or self.eng.blocked else IDLE
        if queued:
            return BUSY
        return IDLE if self.paused else WATCH

    def _refresh(self):
        """Re-render the icon AND the menu.

        pystray.Menu is a snapshot of the items it was given. Building it once
        at startup and calling update_menu() afterwards re-renders the same
        frozen items, so the counts in the menu were whatever they had been
        when the app launched -- which is how a menu offering "Review (5)"
        opened a window correctly reporting that nothing needed reviewing.
        """
        if not self.icon:
            return
        self.icon.icon = _icon_image(self._colour())
        self.icon.title = self._title()
        self.icon.menu = self._build_menu()
        self.icon.update_menu()

    # ---- loop -------------------------------------------------------------
    def _loop(self):
        while not self._stop.is_set():
            if not self.paused:
                try:
                    self.eng.tick()
                except ApiError as e:
                    self.log.error(e.message)
                    if e.terminal:
                        self.eng.blocked = e.message
                except Exception as e:
                    self.log.error("unexpected: %s" % e)
            # _refresh touches the store and the tray API, both of which can
            # fail in ways that used to take the whole worker thread down with
            # them -- and a dead worker means nothing uploads, silently.
            try:
                self._refresh()
            except Exception as e:
                self.log.error("could not update the tray icon: %s" % e)
            self._stop.wait(self.s["poll_interval"])

    # ---- menu actions -----------------------------------------------------
    def on_review(self, *_):
        ui.open_review(self, on_change=self._refresh)

    def on_settings(self, *_):
        ui.open_settings(self)

    def on_status(self, *_):
        ui.open_status(self)

    def on_doctor(self, *_):
        ui.open_doctor(self)

    def on_sign_in(self, *_):
        ui.open_sign_in(self, on_done=self._refresh)

    def on_check_now(self, *_):
        """Don't make someone wait out the poll interval to see if a change
        they just made worked."""
        def go():
            try:
                self.eng.tick()
            except Exception as e:
                self.log.error("check failed: %s" % e)
            self._refresh()
        threading.Thread(target=go, daemon=True).start()

    def on_open_site(self, *_):
        webbrowser.open(config.SITE_BASE + "/wowslegends/replays/")

    def on_open_logs(self, *_):
        d = config.log_dir()
        if os.name == "nt":
            os.startfile(d)                      # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", d], check=False)
        else:
            subprocess.run(["xdg-open", d], check=False)

    def on_pause(self, *_):
        self.paused = not self.paused
        (self.eng.pause if self.paused else self.eng.resume)()
        self.log.info("paused - battles and screenshots from now until you "
                      "resume are not uploaded unless you approve them"
                      if self.paused else "resumed")
        self._refresh()

    def on_retry(self, *_):
        self.eng.unblock()
        self._refresh()

    def on_quit(self, *_):
        self._stop.set()
        if self.icon:
            self.icon.stop()

    # ---- review window ----------------------------------------------------
    def _build_menu(self):
        """Built fresh every refresh -- the counts in it are live."""
        import pystray

        held, queued, waiting = self._counts()
        signed_in = self.client.session.signed_in
        items = [
            pystray.MenuItem(self._title(), None, enabled=False),
            pystray.Menu.SEPARATOR,
        ]
        confirmed = bool(self.s.get("watching_confirmed"))
        if not confirmed:
            items.append(pystray.MenuItem("Check folders and start...",
                                          self.on_settings, default=True))
        if not signed_in:
            # Nothing else matters until this is done, so it goes first and it
            # is the default action.
            items.append(pystray.MenuItem("Sign in...", self.on_sign_in,
                                          default=confirmed))
        else:
            items.append(pystray.MenuItem(
                "Review (%d)" % held, self.on_review,
                enabled=held > 0, default=held > 0))
        items += [
            pystray.MenuItem("Status...", self.on_status,
                             default=confirmed and signed_in and not held),
            pystray.MenuItem("Settings...", self.on_settings),
            pystray.MenuItem("Why isn't it uploading?", self.on_doctor),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Check now", self.on_check_now,
                             enabled=not self.paused),
            pystray.MenuItem("Open Debrief", self.on_open_site),
            pystray.MenuItem("Open log folder", self.on_open_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Resume" if self.paused else "Pause",
                             self.on_pause),
        ]
        if self.eng.blocked:
            items.append(pystray.MenuItem("Try again", self.on_retry))
        items += [pystray.Menu.SEPARATOR,
                  pystray.MenuItem("Quit", self.on_quit)]
        return pystray.Menu(*items)

    # ---- run --------------------------------------------------------------
    def run(self):
        try:
            import pystray
        except ImportError:
            self.log.error("the tray needs pystray (pip install pystray). "
                           "Run without --tray to use the console instead.")
            return 1

        self.icon = pystray.Icon("gd-debrief", _icon_image(self._colour()),
                                 self._title(), menu=self._build_menu())
        threading.Thread(target=self._loop, daemon=True).start()
        if not self.s.get("watching_confirmed"):
            # First run: put the folder check in front of the user rather
            # than waiting for them to find it in a tray menu.
            ui.open_settings(self)
        self.icon.run()
        self._stop.set()
        return 0


def run(settings, store, client, log):
    return Tray(settings, store, client, log).run()
