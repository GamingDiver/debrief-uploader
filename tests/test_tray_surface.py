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
        """Run values in .v; Task Manager's StartupApproved marks in .approved."""
        def __init__(self, fail=False):
            self.v, self.fail, self.approved = {}, fail, {}

        def get(self, n, key=autostart.RUN_KEY):
            return (self.v if key == autostart.RUN_KEY else self.approved).get(n)

        def set(self, n, val):
            if self.fail:
                raise PermissionError("Access is denied")
            self.v[n] = val

        def delete(self, n, key=autostart.RUN_KEY):
            (self.v if key == autostart.RUN_KEY else self.approved).pop(n, None)

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

    def test_built_exe_entry_names_the_exe_alone(self):
        """In the PyInstaller exe, app.py sits in a temp folder deleted on exit."""
        frozen, exe = getattr(sys, "frozen", None), sys.executable
        sys.frozen, sys.executable = True, r"C:\Apps\DebriefUploader\DebriefUploader.exe"
        try:
            cmd = autostart.command()
        finally:
            sys.executable = exe
            if frozen is None:
                del sys.frozen
            else:
                sys.frozen = frozen
        self.assertEqual(cmd, '"C:\\Apps\\DebriefUploader\\DebriefUploader.exe" run --tray --quiet')

    def test_tray_check_names_a_missing_icon(self):
        """`doctor` on the built exe fails release CI through this."""
        real = ui._res
        ui._res = lambda *p: os.path.join(tempfile.gettempdir(), "no-such-dir", *p)
        try:
            ok, why = cli.tray_check()
        finally:
            ui._res = real
        if "cannot load" in why:
            self.skipTest("no tkinter/pystray here: " + why)
        self.assertFalse(ok)
        self.assertIn("icon missing", why)

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

    def test_an_entry_for_another_copy_is_stale_not_enabled(self):
        """2026-10-12: no start after a reboot. An entry naming an old or
        removed copy must not tick the box, and the tray repairs it."""
        reg, tasks = self.Reg(), self.Tasks()
        reg.v[autostart.NAME] = '"C:\\Old\\Gone\\DebriefUploader.exe" run --tray --quiet'
        self.assertEqual(autostart.status(reg=reg, run=tasks), "stale")
        self.assertFalse(autostart.is_enabled(reg=reg, run=tasks))
        self.assertIn("another or a removed copy", autostart.explain(reg=reg, run=tasks))
        self.assertEqual(autostart.repair(reg=reg, run=tasks), "startup entry now points at this copy")
        self.assertEqual(reg.v[autostart.NAME], autostart.command())
        self.assertTrue(autostart.is_enabled(reg=reg, run=tasks))

    def test_repair_never_creates_an_entry(self):
        reg, tasks = self.Reg(), self.Tasks()
        self.assertIsNone(autostart.repair(reg=reg, run=tasks))
        self.assertNotIn(autostart.NAME, reg.v)

    def test_switched_off_in_task_manager_reads_as_off_and_ticking_clears_it(self):
        reg, tasks = self.Reg(), self.Tasks()
        autostart.enable(reg=reg, run=tasks)
        reg.approved[autostart.NAME] = bytes([3]) + bytes(11)
        self.assertEqual(autostart.status(reg=reg, run=tasks), "blocked")
        self.assertFalse(autostart.is_enabled(reg=reg, run=tasks))
        self.assertIsNone(autostart.repair(reg=reg, run=tasks))      # never overrides it
        self.assertIn("Task Manager", autostart.explain(reg=reg, run=tasks))
        ok, _ = autostart.enable(reg=reg, run=tasks)
        self.assertTrue(ok)
        self.assertNotIn(autostart.NAME, reg.approved)
        self.assertTrue(autostart.is_enabled(reg=reg, run=tasks))

    def test_task_manager_on_mark_is_not_blocked(self):
        reg, tasks = self.Reg(), self.Tasks()
        autostart.enable(reg=reg, run=tasks)
        reg.approved[autostart.NAME] = bytes([2]) + bytes(11)
        self.assertEqual(autostart.status(reg=reg, run=tasks), "on")

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

    def test_an_unchanged_tray_is_left_alone(self):
        # Every poll used to rebuild the icon image and the native menu, 43k
        # times a day, and an install reached 16 GB (2026-10-28).
        self.tray._refresh()
        n, icon = self.tray.icon.updates, self.tray.icon.icon
        for _ in range(50):
            self.tray._refresh()
        self.assertEqual(self.tray.icon.updates, n)
        self.assertIs(self.tray.icon.icon, icon)
        # ...but a real change still reaches the tray
        self.store.add_replay(md5="h9", state="held", t_close=1.0)
        self.tray._refresh()
        self.assertEqual(self.tray.icon.updates, n + 1)
        self.assertIn("Review (1)", [str(getattr(m, "text", m)) for m in self.tray.icon.menu.items])

    def test_memory_reading_works_on_this_platform(self):
        mb = tray.process_memory_mb()
        self.assertIsNotNone(mb, "process_memory_mb() returned nothing")
        self.assertGreater(mb, 1)

    def test_memory_is_logged_with_a_number(self):
        lines = []
        self.tray.log.info = lambda m: lines.append(m)
        self.tray.log.error = lambda m: lines.append(m)
        self.tray._log_memory(now=10_000)
        self.tray._log_memory(now=10_001)            # not again within 5 min
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^memory: \d+ MB$")

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
        self.assertEqual(im.getpixel((54, 10))[:3], tray.ATTN)
        self.assertGreater(im.getpixel((32, 40))[3], 0, "the logo is there")

    def test_tray_art_is_the_helmet_filling_the_square(self):
        # Greg 2026-10-11: the tray icon read small; the tray art is the
        # helmet alone, edge to edge (no bubbles, little padding).
        from PIL import Image
        with Image.open(ui._res("tray.png")) as im:
            bbox = im.convert("RGBA").getbbox()
            w, h = im.size
        self.assertGreaterEqual((bbox[3] - bbox[1]) / h, 0.9, "helmet fills the height")

    def test_missing_logo_falls_back_to_a_visible_mark(self):
        real = ui._res
        try:
            tray._res = lambda *p: "/nonexistent/app.ico"
            im = tray._icon_image(tray.WATCH)
        finally:
            tray._res = real
        self.assertIsNotNone(im.getbbox(), "never a fully transparent icon")


class TestCustomTrayMenuFallback(unittest.TestCase):
    """The custom tray popup patches private pystray members. A pystray
    without them must fall back to the native menu, not stop the tray."""

    def test_missing_pystray_internals_fall_back_to_the_native_menu(self):
        import types
        saved = {k: sys.modules.get(k) for k in ("pystray", "pystray._win32")}
        saved_name = tray.os.name
        fake = types.ModuleType("pystray")
        fake_win = types.ModuleType("pystray._win32")

        class Icon:                     # no _on_notify, no _handler
            pass

        fake_win.Icon = Icon
        fake._win32 = fake_win
        sys.modules["pystray"], sys.modules["pystray._win32"] = fake, fake_win
        tray.os.name = "nt"
        try:
            self.assertFalse(tray._install_menus(custom=True))
            self.assertFalse(hasattr(Icon, "_on_notify"))
        finally:
            tray.os.name = saved_name
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_custom_menu_off_installs_nothing(self):
        self.assertFalse(tray._install_menus(custom=False))
