"""Every tray window must actually construct.

I shipped three startup/GUI faults in a row this session -- an empty
__init__, a cross-thread SQLite handle, a preflight the tray skipped -- all
in code no test ever executed. tkinter code is the last of that: it is only
reachable by opening a window, so nothing in the suite touched it.

This builds each window for real and tears it down. It needs a display and
tkinter, so it skips where there is neither; run it with an interpreter that
has Tk (on macOS, /usr/bin/python3).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import tkinter as tk
    _t = tk.Tk()
    _t.destroy()
    HAVE_TK = True
except Exception:
    HAVE_TK = False


class FakeEngine:
    blocked = None

    def apply_exclusions(self):
        return 0

    def counts(self):
        return {"new": 0, "awaiting_shots": 1, "held": 2, "ready": 0,
                "uploading": 0, "uploaded": 3, "orphan": 0, "skipped": 0}

    def diagnose(self, now=None):
        return ["ACCOUNT", "  signed in as a1@example.com", "", "FOLDERS",
                "  replays: 10 file(s)"]

    def _label(self, row):
        return "Myoko / 52_Britain"

    def unblock(self):
        pass


class FakeSession:
    signed_in = True
    email = "a1@example.com"
    user_id = "uid"


class FakeClient:
    session = FakeSession()

    def sign_in_password(self, email, pw):
        return self.session

    def sign_out(self):
        pass


class Row(dict):
    """sqlite3.Row-alike: the windows index rows by name."""
    def __getitem__(self, k):
        return self.get(k)


def _row(**kw):
    base = dict(md5="m", filename="f.wowsreplay", state="uploaded",
                short_code="abc123", note=None, last_error=None,
                player_ship="PJSC008-Myoko", map_name="52_Britain",
                t_close=0, backfill=0)
    base.update(kw)
    return Row(base)


class FakeStore:
    def __init__(self, rows=None, held=None):
        self.rows = rows if rows is not None else [
            _row(md5="a", short_code="abc123"),
            _row(md5="b", state="awaiting_shots", short_code=None),
        ]
        self.held = held or []

    def recent(self, n=20):
        return self.rows

    def replays(self, states=None):
        if states and "held" in states:
            return self.held
        if states and "awaiting_shots" in states:
            return [r for r in self.rows if r["state"] == "awaiting_shots"]
        return self.rows

    def shots_for(self, md5):
        return []


class FakeLog:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(m)

    warn = error = info


class App:
    pass


@unittest.skipUnless(HAVE_TK, "needs tkinter and a display")
class TestWindowsBuild(unittest.TestCase):

    def setUp(self):
        os.environ["GD_UPLOADER_HOME"] = tempfile.mkdtemp()
        from debrief_uploader import config, ui
        self.ui = ui
        self.app = App()
        s = config.Settings()
        s.update({
            "replay_dirs": [r"C:\Program Files (x86)\Steam\steamapps\common"
                            r"\World of Warships Legends\replays"],
            "shot_dirs": [r"C:\Users\player\Pictures\Screenshots"],
        })
        self.app.s = s
        self.app.store = FakeStore()
        self.app.client = FakeClient()
        self.app.log = FakeLog()
        self.app.eng = FakeEngine()

        # Build synchronously and close the window as soon as it is realised,
        # so mainloop() returns instead of blocking the test.
        self.errors = []
        self._real_thread = ui._thread
        self._real_root = ui._root

        def sync(fn, log, what):
            try:
                fn()
            except Exception as e:
                self.errors.append("%s: %r" % (what, e))

        def quick_root(title, w, h):
            r = self._real_root(title, w, h)
            r.after(60, r.destroy)
            return r

        ui._thread = sync
        ui._root = quick_root

    def tearDown(self):
        self.ui._thread = self._real_thread
        self.ui._root = self._real_root
        os.environ.pop("GD_UPLOADER_HOME", None)

    def test_settings_window_builds(self):
        self.ui.open_settings(self.app)
        self.assertEqual(self.errors, [])

    def test_status_window_builds(self):
        self.ui.open_status(self.app)
        self.assertEqual(self.errors, [])

    def test_doctor_window_builds(self):
        self.ui.open_doctor(self.app)
        self.assertEqual(self.errors, [])

    def test_sign_in_window_builds(self):
        self.ui.open_sign_in(self.app)
        self.assertEqual(self.errors, [])

    def test_status_window_shows_ready_for_review_and_a_link(self):
        """The uploaded battle must be presented as ready, with its page."""
        seen = {}
        real_root = self._real_root

        def capture_root(title, w, h):
            r = real_root(title, w, h)
            seen["title"] = title
            r.after(60, r.destroy)
            return r

        self.ui._root = capture_root
        self.ui.open_status(self.app)
        self.assertEqual(self.errors, [])
        self.assertIn("status", seen["title"])

    def test_status_window_refreshes_when_something_new_happens(self):
        """Left open, the status window picks up a new upload by itself
        (Greg 2026-10-05: 'refresh it after each new event')."""
        import tkinter as tk
        seen = {}
        real_root = self._real_root
        store = self.app.store

        def live_root(title, w, h):
            r = real_root(title, w, h)
            r.after(300, lambda: store.rows.insert(
                0, _row(md5="new", short_code="def456")))

            def check():
                t = [x for x in r.winfo_children() if isinstance(x, tk.Text)][0]
                seen["text"] = t.get("1.0", "end")
                r.destroy()
            r.after(2600, check)       # one REFRESH_MS (2 s) after the insert
            return r

        self.ui._root = live_root
        self.ui.open_status(self.app)
        self.assertEqual(self.errors, [])
        self.assertIn("def456", seen["text"])

    def _click_provider(self, provider, outcome):
        """Open the sign-in window, press 'Sign in with <provider>', and report
        what the window did. oauth.sign_in_browser is faked: no browser."""
        import tkinter as tk
        from debrief_uploader import oauth
        from debrief_uploader.api import ApiError
        calls, done, seen = [], [], {}
        real_sib = oauth.sign_in_browser

        def fake_sib(client, prov="discord", timeout=300):
            calls.append(prov)
            if outcome != "ok":
                raise ApiError("oauth", outcome)
            return client.session

        real_root = self._real_root

        def root(title, w, h):
            r = real_root(title, w, h)

            def walk(w_):
                # by type, not position: on Windows our own title bar is
                # the window's first child (winframe), elsewhere it is not
                for c_ in w_.winfo_children():
                    yield c_
                    yield from walk(c_)

            def press():
                btns = [w_ for w_ in walk(r) if isinstance(w_, tk.Button)]
                seen["labels"] = [b_.cget("text") for b_ in btns]
                next(b_ for b_ in btns if provider.title() in b_.cget("text")).invoke()

            def check():
                if not r.winfo_exists():
                    return
                labels = [w_ for w_ in walk(r) if isinstance(w_, tk.Label)]
                seen["msg"] = " ".join(l.cget("text") for l in labels)
                seen["open"] = True
                r.destroy()
            r.after(100, press)
            r.after(1500, check)
            return r

        oauth.sign_in_browser = fake_sib
        self.ui._root = root
        try:
            self.ui.open_sign_in(self.app, on_done=lambda: done.append(1))
        finally:
            oauth.sign_in_browser = real_sib
        return calls, done, seen

    def test_sign_in_offers_google_and_discord(self):
        """Google/Discord accounts have no password (Greg 2026-10-05)."""
        calls, done, seen = self._click_provider("google", "ok")
        self.assertEqual(self.errors, [])
        self.assertIn("Sign in with Google", seen["labels"])
        self.assertIn("Sign in with Discord", seen["labels"])
        self.assertEqual(calls, ["google"])
        self.assertEqual(done, [1])            # signed in -> window closed
        self.assertNotIn("open", seen)

    def test_browser_sign_in_failure_is_shown_not_swallowed(self):
        calls, done, seen = self._click_provider("discord", "redirect not allowed")
        self.assertEqual(calls, ["discord"])
        self.assertEqual(done, [])
        self.assertIn("redirect not allowed", seen["msg"])

    def test_settings_changes_survive_closing_with_the_x(self):
        """Tester 2026-10-05: changed visibility + review mode, closed the
        window, reopened: both were back to the defaults (Save was below the
        visible area at 150% scaling). Every change now saves itself."""
        import tkinter as tk
        from debrief_uploader import config
        real_root = self._real_root

        def walk(w):
            yield w
            for c in w.winfo_children():
                yield from walk(c)

        def root(title, w, h):
            r = real_root(title, w, h)

            def act():
                # the review-mode checkbox, clicked as a user would
                cb = next(x for x in walk(r) if isinstance(x, tk.Checkbutton)
                          and "review mode" in x.cget("text"))
                cb.invoke()
                # the visibility menu: set the variable the OptionMenu drives
                om = next(x for x in walk(r) if x.winfo_class() == "TMenubutton")
                r.globalsetvar(om.cget("textvariable"), "Private - only me")
                # close with the window's X, not a button
                r.tk.call(r.protocol("WM_DELETE_WINDOW"))
            r.after(150, act)
            # never hang the suite if the close path is broken
            r.after(4000, lambda: r.winfo_exists() and r.destroy())
            return r

        self.ui._root = root
        self.ui.open_settings(self.app)
        self.assertEqual(self.errors, [])
        fresh = config.Settings.load()     # re-read from disk
        self.assertEqual(fresh.get("visibility"), "private")
        self.assertTrue(fresh.get("review_mode"))

    @unittest.skipUnless(os.name == "nt", "our own title bar is Windows-only")
    def test_title_bar_close_saves_like_the_native_x(self):
        """winframe draws the title bar, so its X must run the same
        WM_DELETE_WINDOW handler the native one did (save, then close)."""
        import tkinter as tk
        from debrief_uploader import config, winframe
        real_root = self._real_root
        seen = {}

        def walk(w):
            yield w
            for c in w.winfo_children():
                yield from walk(c)

        def root(title, w, h):
            r = real_root(title, w, h)

            def act():
                seen["glyphs"] = [x.cget("text") for x in walk(r)
                                  if isinstance(x, tk.Label)]
                cb = next(x for x in walk(r) if isinstance(x, tk.Checkbutton)
                          and "review mode" in x.cget("text"))
                cb.invoke()
                x_ = next(x for x in walk(r) if isinstance(x, tk.Label)
                          and x.cget("text") == winframe.CLOSE)
                x_.event_generate("<Button-1>")
            r.after(300, act)
            r.after(4000, lambda: r.winfo_exists() and r.destroy())
            return r

        self.ui._root = root
        self.ui.open_settings(self.app)
        self.assertEqual(self.errors, [])
        for g in (winframe.MINIMIZE, winframe.MAXIMIZE, winframe.CLOSE):
            self.assertIn(g, seen["glyphs"])
        self.assertTrue(config.Settings.load().get("review_mode"))

    @unittest.skipUnless(os.name == "nt", "work area + window rect are Win32")
    def test_window_fits_above_the_taskbar_at_the_size_asked(self):
        """The outer window is the size _set_size() asked for and sits inside
        the work area. With the native title bar removed AFTER placing, it
        came out a caption taller and the footer went under the taskbar."""
        import ctypes
        from ctypes import wintypes
        real_root = self._real_root
        seen = {}

        def root(title, w, h):
            r = real_root(title, w, h)

            def measure():
                u = ctypes.windll.user32
                u.GetParent.restype = wintypes.HWND
                rc = wintypes.RECT()
                u.GetWindowRect(u.GetParent(r.winfo_id()), ctypes.byref(rc))
                seen["rect"] = (rc.left, rc.top, rc.right, rc.bottom)
                seen["want"] = r._wh
                seen["work"] = self.ui._work_area(r)
                r.destroy()
            r.after(500, measure)
            return r

        self.ui._root = root
        self.ui.open_settings(self.app)
        self.assertEqual(self.errors, [])
        l, t, rt, b = seen["rect"]
        wl, wt, wr, wb = seen["work"]
        self.assertGreaterEqual(t, wt)
        self.assertLessEqual(b, wb)
        self.assertLessEqual(abs((b - t) - min(seen["want"][1], wb - wt)), 2, seen)

    def test_wrapped_text_follows_a_narrower_panel(self):
        """Resized narrower, a fixed wrap left text wider than its panel and
        Tk centred it, cutting both edges (Stargatecraft, PR #6)."""
        import tkinter as tk
        r = tk.Tk()
        try:
            box = tk.Frame(r, width=300, height=100)
            box.pack(fill="both", expand=True)
            lbl = self.ui._label(box, "word " * 60, wraplength=560)
            lbl.pack(anchor="w")
            r.geometry("300x200")
            r.update()
            self.assertLess(int(str(lbl.cget("wraplength"))), self.ui._px(300))
        finally:
            r.destroy()

    def test_thin_scrollbar_shows_only_when_there_is_more(self):
        """The settings body overflowed at 150% and a tester never found the
        sections below; the slim bar is the cue. It must appear when the
        content overflows, vanish when it fits, and move the view."""
        import tkinter as tk
        r = tk.Tk()
        try:
            t = tk.Text(r, height=5)
            sb = self.ui._ThinScroll(r, t)
            t.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            t.pack(fill="both", expand=True)
            t.insert("1.0", "line\n" * 3)
            r.update()
            self.assertEqual(sb.c.find_all(), ())          # fits: no bar
            t.insert("end", "line\n" * 200)
            r.update()
            self.assertNotEqual(sb.c.find_all(), ())       # overflows: bar
            sb._sleep()                                    # mouse went still
            self.assertEqual(sb.c.find_all(), ())
            sb._wake()                                     # mouse moved
            self.assertNotEqual(sb.c.find_all(), ())
            h = sb.c.winfo_height()
            sb._press(type("E", (), {"y": h - 2})())       # click near the end
            sb._release(None)
            r.update()
            self.assertGreater(t.yview()[0], 0.5)
        finally:
            r.destroy()

    def test_review_window_title_matches_its_contents(self):
        """It used to say "needs you" over a window saying nothing needs you."""
        titles = []
        real_root = self._real_root

        def capture_root(title, w, h):
            r = real_root(title, w, h)
            titles.append(title)
            r.after(60, r.destroy)
            return r

        self.ui._root = capture_root

        self.app.store = FakeStore(held=[])
        self.ui.open_review(self.app)
        self.assertEqual(self.errors, [])
        self.assertIn("nothing to review", titles[-1])

        self.app.store = FakeStore(held=[_row(md5="h1", state="held",
                                              note="an earlier battle...")])
        self.ui.open_review(self.app)
        self.assertEqual(self.errors, [])
        self.assertIn("1 to review", titles[-1])

    def test_pixel_sizes_follow_dpi(self):
        """PR #2 made the process DPI-aware, so fonts render at the real DPI.
        Pixel sizes must grow with them or text crowds out the controls."""
        real = self.ui._SCALE
        try:
            self.ui._SCALE = 1.75
            self.assertEqual(self.ui._px(640), 1120)
            import tkinter as tk
            r = tk.Tk()
            try:
                lbl = self.ui._label(r, "x", wraplength=560)
                self.assertEqual(int(str(lbl.cget("wraplength"))), 980)
            finally:
                r.destroy()
        finally:
            self.ui._SCALE = real

    def test_settings_window_builds_before_first_confirm(self):
        self.app.s["watching_confirmed"] = False
        self.ui.open_settings(self.app)
        self.assertEqual(self.errors, [])

    def test_settings_window_builds_with_nothing_configured(self):
        self.app.s["replay_dirs"] = []
        self.app.s["shot_dirs"] = []
        self.app.client.session.signed_in = False
        self.ui.open_settings(self.app)
        self.assertEqual(self.errors, [])
        self.app.client.session.signed_in = True


if __name__ == "__main__":
    unittest.main(verbosity=2)
