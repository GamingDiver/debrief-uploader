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

    # review actions, recorded
    CHOSEN_NOTE = "screenshots chosen by you"
    CHOSEN_NONE_NOTE = "no screenshots - your choice"

    def __init__(self):
        self.did = []

    def confirm(self, md5):
        self.did.append(("confirm", md5))

    def skip(self, md5):
        self.did.append(("skip", md5))

    def reassign(self, md5, to_md5):
        self.did.append(("reassign", md5, to_md5))

    def set_shots(self, md5, paths):
        self.did.append(("set_shots", md5, list(paths)))

    def upload_without_shots(self, md5):
        self.did.append(("without", md5))

    settling_now = (0, None)        # (count, decides_at) the window shows

    def settling(self, now=None):
        return self.settling_now


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
        self.shots = {}             # md5 -> [shot rows]

    def recent(self, n=20):
        return self.rows

    def replays(self, states=None):
        if states and "held" in states:
            return self.held
        if states and "awaiting_shots" in states:
            return [r for r in self.rows if r["state"] == "awaiting_shots"]
        return self.rows

    def shots_for(self, md5):
        return self.shots.get(md5, [])

    def shot(self, path):
        for md5, rows in self.shots.items():
            for r in rows:
                if r["path"] == path:
                    return r
        return None

    def replay(self, md5):
        return next((r for r in self.held + self.rows if r["md5"] == md5), None)

    def free_shots(self, since=None):
        return getattr(self, "free", [])


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

        ui.core._thread = sync
        ui.core._root = quick_root

    def tearDown(self):
        self.ui.core._thread = self._real_thread
        self.ui.core._root = self._real_root
        os.environ.pop("GD_UPLOADER_HOME", None)
        # Free this test's Tk objects now, on the thread that made them, as
        # ui._thread does in the app. Left to the cyclic GC they are freed on
        # whatever thread runs it next - a background thread left over from
        # an earlier suite - and Tcl aborts the whole run with
        # "Tcl_AsyncDelete: async handler deleted by the wrong thread"
        # (Windows CI, 2026-10-11).
        import gc
        gc.collect()

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

        self.ui.core._root = capture_root
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

            def walk(w_):
                yield w_
                for c_ in w_.winfo_children():
                    yield from walk(c_)

            def check():
                t = next(x for x in walk(r) if isinstance(x, tk.Text))
                seen["text"] = t.get("1.0", "end")
                r.destroy()
            r.after(2600, check)       # one REFRESH_MS (2 s) after the insert
            return r

        self.ui.core._root = live_root
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
        self.ui.core._root = root
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

        self.ui.core._root = root
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

        self.ui.core._root = root
        self.ui.open_settings(self.app)
        self.assertEqual(self.errors, [])
        for g in (winframe.MINIMIZE, winframe.MAXIMIZE, winframe.CLOSE):
            self.assertIn(g, seen["glyphs"])
        self.assertTrue(config.Settings.load().get("review_mode"))

    @unittest.skipUnless(os.name == "nt", "our own title bar is Windows-only")
    def test_every_corner_resizes_and_maximized_x_reaches_the_corner(self):
        """All four corners carry a resize grip (top right was missing,
        Stargatecraft, PR #6). Maximized, the grips step aside so the
        corner pixel belongs to the X."""
        import tkinter as tk
        from debrief_uploader import winframe
        real_root = self._real_root
        seen = {}

        def root(title, w, h):
            r = real_root(title, w, h)

            def at(dx, dy):
                r.update()
                x = r.winfo_rootx() + (r.winfo_width() - 1 if dx else 0)
                y = r.winfo_rooty() + (r.winfo_height() - 1 if dy else 0)
                return r.winfo_containing(x, y)

            def probe():
                seen["codes"] = [getattr(at(dx, dy), "_code", None)
                                 for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1))]
                r.state("zoomed")
                r.update()
                top_right = at(1, 0)
                seen["zoomed_top_right"] = (isinstance(top_right, tk.Label)
                                            and top_right.cget("text"))
                r.destroy()
            r.after(500, probe)
            return r

        self.ui.core._root = root
        self.ui.open_settings(self.app)
        self.assertEqual(self.errors, [])
        self.assertEqual(seen["codes"], [4, 5, 7, 8])
        self.assertEqual(seen["zoomed_top_right"], winframe.CLOSE)

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

        self.ui.core._root = root
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

    @unittest.skipUnless(os.name == "nt", "our own title bar is Windows-only")
    def test_hovered_close_is_one_red_square(self):
        """The top-right resize grips sit over the X's outer edge; hovered,
        they must turn the X's red too, or the corner shows a dark notch
        (Stargatecraft, PR #6, 150% scaling)."""
        import tkinter as tk
        from debrief_uploader import winframe
        real_root = self._real_root
        seen = {}

        def root(title, w, h):
            r = real_root(title, w, h)

            def act():
                x = [w_ for w_ in r.winfo_children()[0].winfo_children()
                     if isinstance(w_, tk.Label) and w_.cget("text") == winframe.CLOSE][0]
                x.event_generate("<Enter>")
                r.update()
                seen["x"] = x.cget("bg")
                seen["grips"] = [g.cget("bg") for g in r._grips[-2:]]
                x.event_generate("<Leave>")
                r.update()
                seen["after"] = [g.cget("bg") for g in r._grips[-2:]]
                r.destroy()
            r.after(400, act)
            return r

        self.ui.core._root = root
        self.ui.open_settings(self.app)
        self.assertEqual(self.errors, [])
        self.assertEqual(seen["grips"], [seen["x"]] * 2)
        self.assertNotEqual(seen["after"][0], seen["x"])

    # ---- one scrolling, reflowing body in every window (PR #6) ----------
    # These drive the window from inside its own mainloop with after():
    # r.update() from outside it never returns on macOS's system Tk 8.5.

    def _walk(self, w):
        yield w
        for c in w.winfo_children():
            yield from self._walk(c)

    def _drive(self, opener, steps):
        """Open a window and run steps, a list of (fn(r), wait_ms), in order
        inside its mainloop, waiting wait_ms after each; the window is
        closed after the last."""
        real_root = self._real_root
        failures = []

        def root(title, w, h):
            r = real_root(title, w, h)
            plan = list(steps)

            def run(i):
                if not r.winfo_exists():
                    return
                if i == len(plan):
                    r.after_cancel(guard)
                    r.destroy()
                    return
                fn, delay = plan[i]
                try:
                    fn(r)
                except Exception as e:      # report, never hang the suite
                    failures.append(e)
                    r.destroy()
                    return
                r.after(delay, lambda: run(i + 1))
            r.after(500, lambda: run(0))
            guard = r.after(15000, lambda: r.winfo_exists() and r.destroy())
            return r

        self.ui.core._root = root
        opener()
        self.assertEqual(self.errors, [])
        if failures:
            raise failures[0]

    def _held(self, n):
        class LongNames(FakeEngine):
            def _label(self, row):
                # a real replay name: long, and without a single space
                return ("20261009_183132_PXSD013-Split-HW26_HW26_%s_"
                        "Hotspot.wowsreplay" % row["md5"])
        self.app.eng = LongNames()
        self.app.store = FakeStore(held=[
            _row(md5="h%d" % i, state="held",
                 note="found before the app was running - upload it?")
            for i in range(n)])

    def test_every_window_has_the_scrolling_body(self):
        """Every window the tray opens scrolls the same way, not only
        Settings (Stargatecraft, PR #6: "other windows need a scroll bar
        too")."""
        seen = {}

        def look(name):
            def fn(r):
                hosts = [w for w in self._walk(r) if hasattr(w, "_scroll")]
                seen[name] = [(h._scroll.c.winfo_manager(),
                               h._scroll.c.winfo_ismapped()) for h in hosts]
            return [(fn, 0)]

        self._drive(lambda: self.ui.open_settings(self.app), look("settings"))
        self._drive(lambda: self.ui.open_status(self.app), look("status"))
        self._drive(lambda: self.ui.open_doctor(self.app), look("doctor"))
        self._drive(lambda: self.ui.open_sign_in(self.app), look("sign in"))
        self.app.store = FakeStore(held=[])
        self._drive(lambda: self.ui.open_review(self.app), look("review, empty"))
        self._held(2)
        self._drive(lambda: self.ui.open_review(self.app), look("review"))
        for name, hosts in seen.items():
            # one body, its bar docked in the reserved gutter
            self.assertEqual(hosts, [("place", 1)], name)
        self.assertEqual(len(seen), 6)

    def test_text_rewraps_when_the_window_is_resized(self):
        """Labels follow the window's width both ways, so text is neither
        cut off when narrower nor kept narrow when wider (Stargatecraft,
        PR #6: "the text continues to be cut off by the resizing")."""
        self._held(3)
        seen = []

        def measure(r):
            lbls = [w for w in self._walk(r) if isinstance(w, tk.Label)
                    and w.winfo_ismapped()
                    and int(str(w.cget("wraplength")) or 0)]
            self.assertTrue(lbls)
            for l in lbls:
                # nothing wider than the space it was given
                self.assertLessEqual(l.winfo_reqwidth(), l.winfo_width() + 1,
                                     l.cget("text"))
                self.assertLessEqual(l.winfo_width(),
                                     l.master.winfo_width(), l.cget("text"))
            seen.append(max(int(str(l.cget("wraplength"))) for l in lbls))

        px = self.ui._px
        self._drive(lambda: self.ui.open_review(self.app), [
            (lambda r: r.geometry("%dx%d" % (px(400), px(400))), 400),
            (measure, 0),
            (lambda r: r.geometry("%dx%d" % (px(900), px(400))), 400),
            (measure, 0),
        ])
        narrow, wide = seen
        self.assertLess(narrow, px(400))
        self.assertGreater(wide, px(600))     # past the old 580 px cap

    def test_overflow_scrolls_instead_of_squashing(self):
        """Six held battles: the window scrolls, every panel keeps its full
        height, and the buttons do not move when the bar appears
        (Stargatecraft, PR #6: the last panel's buttons were cut in half)."""
        self._held(6)
        seen = {}
        px = self.ui._px

        def snap(key):
            def fn(r):
                body = next(w for w in self._walk(r) if hasattr(w, "_scroll"))
                canvas = next(w for w in body.winfo_children()
                              if isinstance(w, tk.Canvas)
                              and w is not body._scroll.c)
                panels = [w for w in self._walk(r) if isinstance(w, tk.Frame)
                          and int(str(w.cget("highlightthickness")) or 0)]
                up = [w for w in self._walk(r) if isinstance(w, tk.Button)
                      and w.cget("text") == "Upload"][0]
                seen[key] = dict(
                    end=canvas.yview()[1],
                    squashed=[p for p in panels
                              if p.winfo_height() < p.winfo_reqheight()],
                    upload=(up.winfo_rootx() - r.winfo_rootx(),
                            up.winfo_width()))
            return fn

        self._drive(lambda: self.ui.open_review(self.app), [
            (lambda r: r.geometry("%dx%d" % (px(600), px(2000))), 400),
            (snap("tall"), 0),
            (lambda r: r.geometry("%dx%d" % (px(600), px(360))), 400),
            (snap("short"), 0),
        ])
        self.assertLess(seen["short"]["end"], 1.0)        # it scrolls
        self.assertEqual(seen["short"]["squashed"], [])
        self.assertEqual(seen["tall"]["upload"], seen["short"]["upload"])

    def test_edges_resize_on_one_axis_and_reach_past_the_buttons(self):
        """Each edge is a one-axis grip with its own cursor, including the
        strip above the title bar's buttons and the X's right side, where
        there was none (Stargatecraft, PR #6). The buttons still click
        below the strip, and a hovered X tints every grip over it."""
        import tkinter as tk_
        from debrief_uploader import winframe
        real_frame = tk_.Frame
        if os.name != "nt":
            # the size_* cursors exist only in Windows Tk; elsewhere test
            # the layout with a stand-in cursor and remember the real one
            class Frame(real_frame):
                def __init__(self, *a, **kw):
                    want = kw.get("cursor", "")
                    if want.startswith("size_"):
                        kw["cursor"] = "crosshair"
                    real_frame.__init__(self, *a, **kw)
                    self.want = want
            tk_.Frame = Frame
        seen = {}
        try:
            r = tk_.Tk()
            r.geometry("520x420+40+40")
            winframe.frame(r, "t", self.ui._px, self.ui.BG,
                           self.ui._res("app.ico"))
            self.ui._Body(r)

            def probe():
                try:
                    for g in r._grips:
                        g.lift()
                    W, H = r.winfo_width(), r.winfo_height()

                    def at(x, y):
                        w = r.winfo_containing(r.winfo_rootx() + x,
                                               r.winfo_rooty() + y)
                        cur = getattr(w, "want", None) or (
                            w.cget("cursor") if w is not None else None)
                        return getattr(w, "_code", None), cur, w
                    px = self.ui._px
                    for name, xy in {"left": (1, H // 2),
                                     "right": (W - 2, H // 2),
                                     "top": (W // 3, 1),
                                     "bottom": (W // 2, H - 2),
                                     "beside the X": (W - 2, px(20))}.items():
                        seen[name] = at(*xy)[:2]
                    bar = r.winfo_children()[0]
                    for b in bar.winfo_children():
                        if isinstance(b, tk_.Label) and b.cget("text") in (
                                winframe.CLOSE, winframe.MAXIMIZE,
                                winframe.MINIMIZE):
                            mid = b.winfo_rootx() - r.winfo_rootx() + b.winfo_width() // 2
                            seen["above " + b.cget("text")] = at(mid, 1)[:2]
                            seen["on " + b.cget("text")] = at(mid, px(16))[2] is b
                            if b.cget("text") == winframe.CLOSE:
                                b.event_generate("<Enter>")
                                seen["hover"] = ({g.cget("bg") for g in b._grips},
                                                 b.cget("bg"), len(b._grips))
                                b.event_generate("<Leave>")
                finally:
                    r.destroy()
            r.after(500, probe)
            r.mainloop()
        finally:
            tk_.Frame = real_frame
        self.assertEqual(seen["left"], (1, "size_we"))
        self.assertEqual(seen["right"], (2, "size_we"))
        self.assertEqual(seen["beside the X"], (2, "size_we"))
        self.assertEqual(seen["top"], (3, "size_ns"))
        self.assertEqual(seen["bottom"], (6, "size_ns"))
        for g in (winframe.CLOSE, winframe.MAXIMIZE, winframe.MINIMIZE):
            self.assertEqual(seen["above " + g], (3, "size_ns"), g)
            self.assertTrue(seen["on " + g], g)
        tints, x_bg, n = seen["hover"]
        self.assertEqual(tints, {x_bg})
        self.assertEqual(n, 4)       # top strip, right side, the corner's L

    # ---- Review: previews that scale, live updates, chosen screenshots --
    # (Greg, PR #6: "the images dont scale", "The Review window does not
    # update", "i dont trust the auto matching")

    def _shots_held(self, n_shots=2, extra_files=0):
        """One held battle with n_shots matched screenshots, real 16:9 image
        files on disk, plus extra_files more in the screenshots folder."""
        import time as _t
        from PIL import Image
        d = tempfile.mkdtemp()
        now = _t.time()
        t_close = now - 600
        row = _row(md5="h1", state="held", note="review mode is on",
                   t_close=t_close)

        def shot(name, t):
            p = os.path.join(d, name)
            Image.new("RGB", (1920, 1080), (40, 80, 120)).save(p)
            os.utime(p, (t, t))
            return Row(dict(path=p, t=t, replay_md5="h1", uploaded=0,
                            ignored=0))
        mine = [shot("auto_%d.png" % i, t_close + 40 + 7 * i)
                for i in range(n_shots)]
        extra = [shot("extra_%d.png" % i, t_close + 300 + 10 * i)
                 for i in range(extra_files)]
        self.app.store = FakeStore(held=[row])
        self.app.store.shots = {"h1": mine}
        self.app.s.update({"shot_dirs": [d], "shot_min_kb": 0,
                           "watching_since": None})
        return row, mine, [x["path"] for x in extra]

    def _thumbs(self, r):
        return [w._thumbs for w in self._walk(r) if hasattr(w, "_thumbs")]

    def test_review_previews_scale_with_the_window(self):
        self._shots_held(2)
        px = self.ui._px
        seen = {}

        def measure(key):
            def fn(r):
                th = self._thumbs(r)
                self.assertEqual(len(th), 1)
                seen[key] = [(c["photo"].width(), c["photo"].height())
                             for c in th[0].cells]
            return fn

        self._drive(lambda: self.ui.open_review(self.app), [
            (lambda r: r.geometry("%dx%d" % (px(420), px(600))), 500),
            (measure("narrow"), 0),
            (lambda r: r.geometry("%dx%d" % (px(1300), px(600))), 500),
            (measure("wide"), 0),
            # wider than any CI screen lets the window be (Windows CI
            # clamps it to ~1024): lay out for a huge panel directly
            (lambda r: self._thumbs(r)[0].fit(px(3000)), 0),
            (measure("huge"), 0),
        ])
        for key in ("narrow", "wide", "huge"):
            for w, h in seen[key]:
                self.assertAlmostEqual(h / float(w), 1080 / 1920.0, places=1)
                self.assertGreaterEqual(w, px(150))
                self.assertLessEqual(w, px(640))
        self.assertGreater(seen["wide"][0][0], seen["narrow"][0][0] * 1.5)
        self.assertEqual(seen["huge"][0][0], px(640))        # capped
        self.assertGreater(seen["wide"][0][0], 150)   # was a fixed 150x84

    def test_clicking_a_preview_opens_it_large_and_escape_closes(self):
        self._shots_held(1)
        seen = {}

        def click(r):
            th = self._thumbs(r)[0]
            seen["thumb"] = th.cells[0]["photo"].width()
            th.cells[0]["img"].event_generate("<Button-1>")

        def look(r):
            views = [w for w in r.winfo_children() if isinstance(w, tk.Toplevel)]
            self.assertEqual(len(views), 1)
            v = views[0]
            seen["size"] = v._state["size"]
            v.event_generate("<Escape>")

        def gone(r):
            seen["left"] = [w for w in r.winfo_children()
                            if isinstance(w, tk.Toplevel)]

        self._drive(lambda: self.ui.open_review(self.app), [
            (lambda r: None, 300), (click, 700), (look, 200), (gone, 0)])
        self.assertGreater(seen["size"][0], seen["thumb"])
        self.assertAlmostEqual(seen["size"][1] / float(seen["size"][0]),
                               1080 / 1920.0, places=1)
        self.assertEqual(seen["left"], [])

    def _texts(self, r):
        return " ".join(w.cget("text") for w in self._walk(r)
                        if isinstance(w, (tk.Label, tk.Button)))

    def _cards(self, r, waiting=False):
        """The held battles' cards (or, waiting=True, the cards of battles
        still waiting for scorecards)."""
        return [w for w in self._walk(r) if isinstance(w, tk.Frame)
                and int(str(w.cget("highlightthickness")) or 0)
                and w.winfo_ismapped()
                and bool(getattr(w, "_waiting", False)) == waiting]

    def test_review_updates_itself_and_its_title(self):
        """Battles held after it opened appear, resolved ones go, and the
        title always counts what is shown."""
        store = FakeStore(held=[_row(md5="h1", state="held", note="a")])
        self.app.store = store
        seen = {}

        def look(key):
            def fn(r):
                seen[key] = (r.title(), len(self._cards(r)), self._texts(r))
            return fn

        self._drive(lambda: self.ui.open_review(self.app), [
            (look("one"), 0),
            (lambda r: store.held.append(
                _row(md5="h2", state="held", note="b")), 1400),
            (look("two"), 0),
            (lambda r: store.held.clear(), 1400),
            (look("none"), 0),
        ])
        self.assertIn("1 to review", seen["one"][0])
        self.assertEqual(seen["one"][1], 1)
        self.assertIn("2 to review", seen["two"][0])
        self.assertEqual(seen["two"][1], 2)
        self.assertIn("nothing to review", seen["none"][0])
        self.assertEqual(seen["none"][1], 0)
        self.assertIn("Nothing to review.", seen["none"][2])

    def test_upload_redraws_instead_of_leaving_an_empty_window(self):
        """Greg's screenshot: "1 to review" over an empty window, after the
        one panel was resolved and only deleted."""
        store = FakeStore(held=[_row(md5="h1", state="held", note="a")])
        self.app.store = store

        class Resolves(FakeEngine):
            def confirm(self, md5):
                store.held[:] = [x for x in store.held if x["md5"] != md5]
        self.app.eng = Resolves()
        seen = {}

        def press(r):
            next(w for w in self._walk(r) if isinstance(w, tk.Button)
                 and w.cget("text") == "Upload").invoke()
            # no wait: the redraw is immediate, not the next poll
            seen["after"] = (r.title(), self._texts(r))

        self._drive(lambda: self.ui.open_review(self.app), [(press, 0)])
        self.assertIn("nothing to review", seen["after"][0])
        self.assertIn("Nothing to review.", seen["after"][1])

    def test_empty_review_says_so(self):
        self.app.store = FakeStore(held=[])
        seen = {}
        self._drive(lambda: self.ui.open_review(self.app), [
            (lambda r: seen.update(t=self._texts(r), title=r.title()), 0)])
        self.assertIn("Nothing to review.", seen["t"])
        self.assertIn("nothing to review", seen["title"])

    def test_opening_review_again_brings_the_open_one_forward(self):
        self.app.store = FakeStore(held=[_row(md5="h1", state="held")])
        roots = []
        seen = {}
        real_root = self._real_root

        def counting(title, w, h):
            roots.append(title)
            return real_root(title, w, h)
        self._real_root = counting

        def again(r):
            # what a second tray click does; with the sync _thread a
            # second window would be built right here, inside this one
            self.ui.open_review(self.app)
            seen["asked"] = self.ui._REVIEW["front"]

        def later(r):
            seen["served"] = not self.ui._REVIEW["front"]

        try:
            self._drive(lambda: self.ui.open_review(self.app),
                        [(again, 600), (later, 0)])
            self.assertEqual(len(roots), 1)
            self.assertTrue(seen["asked"])
            self.assertTrue(seen["served"])
            self.assertFalse(self.ui._REVIEW["open"])
            # closed, it opens again
            self._drive(lambda: self.ui.open_review(self.app), [])
            self.assertEqual(len(roots), 2)
        finally:
            self._real_root = real_root

    def test_review_says_which_screenshots_and_why(self):
        self._shots_held(2)
        seen = {}
        self._drive(lambda: self.ui.open_review(self.app), [
            (lambda r: seen.update(t=self._texts(r)), 0)])
        self.assertIn("MATCHED AUTOMATICALLY - 2 screenshots, the first "
                      "taken 40 s after the battle", seen["t"])
        self.assertIn("47 s after the battle", seen["t"])
        self.assertIn("Change screenshots...", seen["t"])
        self.assertIn("Upload without screenshots", seen["t"])

    def test_picker_choice_replaces_the_matched_screenshots(self):
        row, mine, extra = self._shots_held(2, extra_files=2)
        seen = {}

        def open_picker(r):
            next(w for w in self._walk(r) if isinstance(w, tk.Button)
                 and w.cget("text") == "Change screenshots...").invoke()

        def choose(r):
            p = r._picker
            ticked = {path: v.get() for path, _, v, _ in p._tiles}
            seen["listed"] = [path for path, _, _, _ in p._tiles]
            seen["ticked"] = sorted(k for k, v in ticked.items() if v)
            for path, _, v, _ in p._tiles:
                # keep the first matched one, swap the second for a new one
                v.set(path in (mine[0]["path"], extra[1]))
            p._apply()
            seen["closed"] = not p.winfo_exists()

        self._drive(lambda: self.ui.open_review(self.app), [
            (open_picker, 300), (choose, 0)])
        auto = sorted(x["path"] for x in mine)
        self.assertEqual(seen["ticked"], auto)       # matched ones pre-ticked
        # newest first
        self.assertEqual(seen["listed"],
                         sorted(seen["listed"],
                                key=lambda p: -os.path.getmtime(p)))
        self.assertEqual(set(seen["listed"]), set(auto) | set(extra))
        self.assertTrue(seen["closed"])
        # oldest first, as the upload sends them
        self.assertIn(("set_shots", "h1", [mine[0]["path"], extra[1]]),
                      self.app.eng.did)

    def test_picker_refuses_more_than_the_upload_takes(self):
        row, mine, extra = self._shots_held(2, extra_files=4)
        seen = {}

        def go(r):
            next(w for w in self._walk(r) if isinstance(w, tk.Button)
                 and w.cget("text") == "Change screenshots...").invoke()
            p = r._picker
            for _, _, v, _ in p._tiles:
                v.set(False)
            boxes = [w for w in self._walk(p) if isinstance(w, tk.Checkbutton)]
            for cb in boxes:
                cb.invoke()                  # as a user would, one by one
            seen["ticked"] = sum(1 for _, _, v, _ in p._tiles if v.get())

        self._drive(lambda: self.ui.open_review(self.app), [(go, 0)])
        self.assertEqual(seen["ticked"], self.app.s["max_shots"])

    def test_upload_without_screenshots_is_one_click(self):
        self._shots_held(2)
        self._drive(lambda: self.ui.open_review(self.app), [
            (lambda r: next(w for w in self._walk(r)
                            if isinstance(w, tk.Button) and
                            w.cget("text") == "Upload without screenshots"
                            ).invoke(), 0)])
        self.assertIn(("without", "h1"), self.app.eng.did)

    # ---- Review, second round (Stargatecraft, PR #6 retest) -------------

    def test_waiting_battles_show_at_once_with_their_actions(self):
        """"It doesnt update new scoreboards on its own without
        scoreboards": a battle with no scorecards only became a card after
        orphan_after (15 min). It is listed, and can be settled, at once."""
        import time as _t
        self.app.store = FakeStore(rows=[_row(
            md5="w1", state="awaiting_shots", t_close=_t.time() - 60)])
        seen = {}

        def look(r):
            seen["held"] = len(self._cards(r))
            seen["waiting"] = len(self._cards(r, waiting=True))
            seen["t"] = self._texts(r)
            next(w for w in self._walk(r) if isinstance(w, tk.Button)
                 and w.cget("text") == "Upload without screenshots").invoke()

        self._drive(lambda: self.ui.open_review(self.app), [(look, 0)])
        self.assertEqual(seen["held"], 0)
        self.assertEqual(seen["waiting"], 1)
        self.assertIn("WAITING FOR SCORECARDS", seen["t"])
        self.assertIn("Choose screenshots...", seen["t"])
        self.assertIn("Nothing to review.", seen["t"])
        self.assertIn(("without", "w1"), self.app.eng.did)

    def test_screenshots_still_settling_are_explained(self):
        """"it did appear ... but it took a minute-ish": the matcher waits
        in case another screenshot is coming; the window says so, and when."""
        import time as _t
        self.app.store = FakeStore(rows=[])
        self.app.eng.settling_now = (1, _t.time() + 25)
        seen = {}
        self._drive(lambda: self.ui.open_review(self.app), [
            (lambda r: seen.update(t=self._texts(r)), 0)])
        self.assertIn("1 screenshot just taken - it is matched to a battle at",
                      seen["t"])

    def test_picker_closes_when_its_battle_is_resolved_elsewhere(self):
        """A review finished while the picker was open left it choosing for
        a battle that was gone; Use these then failed."""
        row, mine, extra = self._shots_held(2)
        store = self.app.store
        seen = {}

        def open_picker(r):
            next(w for w in self._walk(r) if isinstance(w, tk.Button)
                 and w.cget("text") == "Change screenshots...").invoke()
            seen["picker"] = r._picker
            store.held.clear()          # uploaded from somewhere else

        def look(r):
            seen["open"] = seen["picker"].winfo_exists()

        self._drive(lambda: self.ui.open_review(self.app), [
            (open_picker, 1600), (look, 0)])
        self.assertFalse(seen["open"])

    def test_picker_greys_a_screenshot_sent_with_another_battle(self):
        row, mine, extra = self._shots_held(2, extra_files=1)
        store = self.app.store
        seen = {}

        def open_picker(r):
            next(w for w in self._walk(r) if isinstance(w, tk.Button)
                 and w.cget("text") == "Change screenshots...").invoke()
            p = r._picker
            for path, _, v, _ in p._tiles:
                if path == extra[0]:
                    v.set(True)
            store.rows.append(_row(md5="o1", state="uploaded"))
            store.shots["o1"] = [Row(dict(path=extra[0], t=0, replay_md5="o1",
                                          uploaded=1, ignored=0))]
            p._watch()
            seen["ticked"] = [path for path, _, v, _ in p._tiles if v.get()]
            boxes = {}
            for w in self._walk(p):
                if isinstance(w, tk.Checkbutton):
                    boxes[str(w.cget("variable"))] = w.cget("state")
            seen["states"] = boxes
            seen["t"] = self._texts(p)

        self._drive(lambda: self.ui.open_review(self.app), [(open_picker, 0)])
        self.assertNotIn(extra[0], seen["ticked"])
        self.assertIn("disabled", seen["states"].values())
        self.assertIn("already sent with", seen["t"])

    def test_review_window_title_matches_its_contents(self):
        """It used to say "needs you" over a window saying nothing needs you."""
        titles = []
        real_root = self._real_root

        def capture_root(title, w, h):
            r = real_root(title, w, h)
            titles.append(title)
            r.after(60, r.destroy)
            return r

        self.ui.core._root = capture_root

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
            self.ui.core._SCALE = 1.75
            self.assertEqual(self.ui._px(640), 1120)
            import tkinter as tk
            r = tk.Tk()
            try:
                lbl = self.ui._label(r, "x", wraplength=560)
                self.assertEqual(int(str(lbl.cget("wraplength"))), 980)
            finally:
                r.destroy()
        finally:
            self.ui.core._SCALE = real

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
