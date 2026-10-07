"""The tray must be able to do everything the CLI can.

The user's requirement: configure and run entirely from the tray icon. So the
menu has to reach sign-in, folder configuration, settings, status, diagnosis
and review -- and starting with no folders configured must open the tray so
they can fix it there, not refuse to start.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from debrief_uploader import autostart, cli, config, single_instance, tray, ui
from debrief_uploader.log import Log
from debrief_uploader.state import Store
from tests.test_cli import FakeClient


class TestTrayActions(unittest.TestCase):

    def test_every_menu_action_exists(self):
        for name in ("on_review", "on_settings", "on_status", "on_doctor",
                     "on_sign_in", "on_check_now", "on_open_site",
                     "on_open_logs", "on_pause", "on_retry", "on_quit"):
            self.assertTrue(callable(getattr(tray.Tray, name, None)), name)

    def test_ui_exposes_every_window_the_menu_opens(self):
        for name in ("open_settings", "open_status", "open_doctor",
                     "open_sign_in"):
            self.assertTrue(callable(getattr(ui, name, None)), name)

    def test_visibility_choices_match_the_site(self):
        values = [v for _, v in ui.VISIBILITY]
        self.assertEqual(sorted(v for v in values if v),
                         ["fleet", "private", "public"])
        self.assertIn("", values, "must offer 'use my site default'")


class TestStartsUnconfigured(unittest.TestCase):
    """With no folders set, the console run refuses -- but the tray starts, so
    the folders can be chosen in it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["GD_UPLOADER_HOME"] = self.tmp
        s = config.Settings()
        s.update({"replay_dirs": [], "shot_dirs": [], "_dirs_pinned": True})
        s.save()
        self.log = Log(None, echo=False)

    def tearDown(self):
        os.environ.pop("GD_UPLOADER_HOME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_console_run_refuses(self):
        s = config.Settings.load()
        self.assertFalse(cli._preflight(s, FakeClient(), self.log, fatal=True))

    def test_tray_run_is_allowed_and_says_what_is_wrong(self):
        s = config.Settings.load()
        self.assertTrue(cli._preflight(s, FakeClient(), self.log, fatal=False))
        text = "\n".join(self.log.lines)
        self.assertIn("no replay folder", text)
        self.assertIn("Settings", text, "must point at where to fix it")


class TestSingleInstance(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["GD_UPLOADER_HOME"] = self.tmp

    def tearDown(self):
        os.environ.pop("GD_UPLOADER_HOME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_second_acquire_is_refused(self):
        a = single_instance.Lock("test-a").acquire()
        try:
            with self.assertRaises(single_instance.AlreadyRunning):
                single_instance.Lock("test-a").acquire()
        finally:
            a.release()

    def test_lock_is_reusable_after_release(self):
        single_instance.Lock("test-b").acquire().release()
        single_instance.Lock("test-b").acquire().release()

    def test_context_manager_releases(self):
        with single_instance.Lock("test-c"):
            pass
        single_instance.Lock("test-c").acquire().release()

    def test_it_opens_no_network_socket(self):
        """A background tray app must not make the OS ask about incoming
        connections. The first implementation bound a loopback socket and
        macOS raised a local-network prompt; on Windows that is a Defender
        Firewall dialog, and a user who clicks Block gets a lock that
        silently never works."""
        import inspect
        src = inspect.getsource(single_instance)
        code = "\n".join(l for l in src.splitlines()
                          if not l.strip().startswith("#")
                          and '"""' not in l)
        self.assertNotIn("import socket", code)
        self.assertNotIn("bind(", code)


class TestAutostart(unittest.TestCase):
    """Start-at-login is a per-user Run registry entry (no admin rights).
    Registry and schtasks are injected, so this runs on any machine."""

    class Reg:
        def __init__(self, fail=False):
            self.v, self.fail = {}, fail

        def get(self, n):
            return self.v.get(n)

        def set(self, n, val):
            if self.fail:
                raise PermissionError("Access is denied")
            self.v[n] = val

        def delete(self, n):
            self.v.pop(n, None)

    class Tasks:
        """schtasks: rc for /Query (legacy task present?) and /Delete."""
        def __init__(self, exists=False, delete_rc=0):
            self.exists, self.delete_rc, self.calls = exists, delete_rc, []

        def __call__(self, args):
            self.calls.append(args)
            rc = (0 if self.exists else 1) if "/Query" in args else self.delete_rc
            if "/Delete" in args and rc == 0:
                self.exists = False

            class R:
                returncode = rc
                stdout = stderr = ""
            return R()

    def setUp(self):
        self._sup = autostart.supported
        autostart.supported = lambda: True

    def tearDown(self):
        autostart.supported = self._sup

    def test_enable_needs_no_admin_and_writes_the_run_entry(self):
        reg, tasks = self.Reg(), self.Tasks()
        ok, why = autostart.enable(reg=reg, run=tasks)
        self.assertTrue(ok, why)
        cmd = reg.v[autostart.NAME]
        self.assertIn("app.py", cmd)
        self.assertIn("run --tray --quiet", cmd)
        self.assertFalse(any("/Create" in c for c in tasks.calls))   # no schtasks
        self.assertTrue(autostart.is_enabled(reg=reg, run=tasks))

    def test_disable_removes_the_entry_and_a_legacy_task(self):
        reg, tasks = self.Reg(), self.Tasks(exists=True)
        autostart.enable(reg=reg, run=tasks)
        ok, _ = autostart.disable(reg=reg, run=tasks)
        self.assertTrue(ok)
        self.assertNotIn(autostart.NAME, reg.v)
        self.assertTrue(any("/Delete" in c for c in tasks.calls))
        self.assertFalse(autostart.is_enabled(reg=reg, run=tasks))

    def test_a_legacy_scheduled_task_still_counts_as_enabled(self):
        self.assertTrue(autostart.is_enabled(reg=self.Reg(), run=self.Tasks(exists=True)))

    def test_failure_is_reported_not_swallowed(self):
        ok, why = autostart.enable(reg=self.Reg(fail=True), run=self.Tasks())
        self.assertFalse(ok)
        self.assertIn("Access is denied", why)

    def test_real_registry_round_trip_on_windows(self):
        """The winreg calls themselves, under a throwaway value name."""
        if os.name != "nt":
            self.skipTest("Windows only")
        reg, name = autostart._Registry(), "GamingDiver Debrief Uploader (test)"
        try:
            reg.set(name, "x")
            self.assertEqual(reg.get(name), "x")
        finally:
            reg.delete(name)
        self.assertIsNone(reg.get(name))
        reg.delete(name)                       # deleting twice is harmless

    def test_unsupported_platform_is_honest(self):
        autostart.supported = lambda: False
        ok, why = autostart.enable()
        self.assertFalse(ok)
        self.assertIn("Windows", why)


class TestLogSurvivesNoConsole(unittest.TestCase):
    """pythonw.exe -- how the tray runs -- sets sys.stdout to None."""

    def test_echo_is_disabled_when_there_is_no_stdout(self):
        import debrief_uploader.log as logmod
        real = logmod.sys.stdout
        logmod.sys.stdout = None
        try:
            lg = logmod.Log(None, echo=True)
            self.assertFalse(lg.echo)
            lg.info("this must not raise")
            self.assertEqual(len(lg.lines), 1)
        finally:
            logmod.sys.stdout = real


class TestLauncherDetaches(unittest.TestCase):
    """The window must not sit there for the rest of the session."""

    def setUp(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "Start-DebriefUploader.cmd")) as f:
            self.cmd = f.read()

    def test_it_starts_detached_with_the_windowless_interpreter(self):
        self.assertIn("start \"\"", self.cmd)
        self.assertIn("pythonw.exe", self.cmd)

    def test_it_exits_immediately_after_handing_off(self):
        """Nothing blocking may sit between the hand-off and the exit.

        (The `pause` further down is in the :fail handler, where a window that
        stays open is exactly what you want.)"""
        lines = [l.strip() for l in self.cmd.splitlines()]
        i = next(i for i, l in enumerate(lines)
                 if l.startswith('start ""') and "%PYW%" in l)
        after = [l for l in lines[i + 1:] if l and not l.startswith("REM")]
        self.assertEqual(after[0], "exit /b 0",
                         "the launcher must exit the moment the tray is away")

    def test_it_no_longer_blocks_on_interactive_login(self):
        self.assertNotIn("debrief_uploader login", self.cmd,
                         "sign-in belongs in the tray now")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class FakePystray:
    """Enough of pystray to build a menu without the real package."""
    SEPARATOR = object()

    class MenuItem:
        def __init__(self, text, action, enabled=True, default=False, **kw):
            self.text, self.action = text, action
            self.enabled, self.default = enabled, default

    class Menu:
        SEPARATOR = None

        def __init__(self, *items):
            self.items = items

        def labels(self):
            return [i.text for i in self.items
                    if isinstance(i, FakePystray.MenuItem)]

    def __init__(self):
        FakePystray.Menu.SEPARATOR = FakePystray.SEPARATOR


class FakeIcon:
    def __init__(self):
        self.menu = None
        self.title = ""
        self.icon = None
        self.updates = 0

    def update_menu(self):
        self.updates += 1


class TestMenuIsNotStale(unittest.TestCase):
    """pystray.Menu is a snapshot of the items it was handed.

    Building it once at startup and calling update_menu() afterwards
    re-renders the SAME items, so the counts were frozen at launch -- which is
    how a menu offering "Review (5)" opened a window correctly reporting that
    nothing needed reviewing.
    """

    def setUp(self):
        import sys as _s
        self.tmp = tempfile.mkdtemp()
        os.environ["GD_UPLOADER_HOME"] = self.tmp
        self._real = _s.modules.get("pystray")
        _s.modules["pystray"] = FakePystray()

        s = config.Settings()
        s.update({"replay_dirs": [self.tmp], "shot_dirs": [self.tmp],
                  "watching_confirmed": True})
        self.store = Store(os.path.join(self.tmp, "state.db"))
        self.tray = tray.Tray(s, self.store, FakeClient(), Log(None, echo=False))
        self.tray.icon = FakeIcon()

    def tearDown(self):
        import sys as _s
        if self._real is None:
            _s.modules.pop("pystray", None)
        else:
            _s.modules["pystray"] = self._real
        self.store.close()
        os.environ.pop("GD_UPLOADER_HOME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _labels(self):
        return self.tray._build_menu().labels()

    def test_first_run_puts_the_folder_check_first(self):
        self.tray.s["watching_confirmed"] = False
        self.tray._refresh()
        labels = [str(getattr(m, "text", m)) for m in self.tray.icon.menu.items]
        self.assertIn("check your folders", labels[0])
        self.assertIn("Check folders and start...", labels)

    def test_counts_track_the_store(self):
        self.assertIn("Review (0)", self._labels())
        for i in range(3):
            self.store.add_replay(md5="h%d" % i, state="held", t_close=1.0)
        self.assertIn("Review (3)", self._labels())

    def test_refresh_rebuilds_rather_than_re_rendering(self):
        self.tray._refresh()
        first = self.tray.icon.menu
        self.store.add_replay(md5="h1", state="held", t_close=1.0)
        self.tray._refresh()
        self.assertIsNot(self.tray.icon.menu, first,
                         "a refresh must build a NEW menu, not re-render the "
                         "one from startup")
        self.assertIn("Review (1)", self.tray.icon.menu.labels())
        self.assertGreaterEqual(self.tray.icon.updates, 2)

    def test_review_is_disabled_when_there_is_nothing_to_review(self):
        item = next(i for i in self.tray._build_menu().items
                    if isinstance(i, FakePystray.MenuItem)
                    and i.text.startswith("Review"))
        self.assertFalse(item.enabled)
        self.assertFalse(item.default)

    def test_signed_out_offers_sign_in_first(self):
        self.tray.client.session.access = None
        self.tray.client.session.refresh = None
        labels = self._labels()
        self.assertIn("Sign in...", labels)
        self.assertLess(labels.index("Sign in..."), labels.index("Settings..."))


class TestTrayIcon(unittest.TestCase):
    """PR #3 replaced the drawn mark with the app logo plus a status dot."""

    def setUp(self):
        tray._BASE = None

    def tearDown(self):
        tray._BASE = None

    def test_icon_file_ships_every_windows_size(self):
        from PIL import Image
        with Image.open(ui._res("app.ico")) as im:
            sizes = set(im.info["sizes"])
        for want in ((16, 16), (32, 32), (48, 48), (256, 256)):
            self.assertIn(want, sizes)

    def test_status_dot_is_drawn_over_the_logo(self):
        im = tray._icon_image(tray.ATTN)
        self.assertEqual(im.size, (64, 64))
        self.assertEqual(im.getpixel((48, 16))[:3], tray.ATTN)
        self.assertGreater(im.getpixel((32, 40))[3], 0, "the logo is there")

    def test_missing_logo_falls_back_to_a_visible_mark(self):
        real = ui._res
        try:
            tray._res = lambda *p: "/nonexistent/app.ico"
            im = tray._icon_image(tray.WATCH)
        finally:
            tray._res = real
        self.assertIsNotNone(im.getbbox(), "never a fully transparent icon")
