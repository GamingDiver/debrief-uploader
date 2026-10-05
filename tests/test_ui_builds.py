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

    def test_settings_window_builds_with_nothing_configured(self):
        self.app.s["replay_dirs"] = []
        self.app.s["shot_dirs"] = []
        self.app.client.session.signed_in = False
        self.ui.open_settings(self.app)
        self.assertEqual(self.errors, [])
        self.app.client.session.signed_in = True


if __name__ == "__main__":
    unittest.main(verbosity=2)
