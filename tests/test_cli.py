"""CLI wiring -- in particular that starting WITH a tray icon tells the user
the same things as starting without one.

The tray branch used to return before every preflight check, so the way
almost everyone starts the app was also the way that reported nothing: no
folder counts, no missing-folder warning, no sign-in error. Silence looked
like health.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from debrief_uploader import cli, config
from debrief_uploader.api import Session
from debrief_uploader.log import Log


class FakeClient:
    def __init__(self, signed_in=True):
        self.session = Session(access="a" if signed_in else None,
                               refresh="r" if signed_in else None,
                               expires_at=time.time() + 3600,
                               user_id="uid", email="a1@example.com")


class TestPreflight(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = Log(None, echo=False)
        self.s = config.Settings()
        self.rdir = os.path.join(self.tmp, "replays")
        self.sdir = os.path.join(self.tmp, "shots")
        os.makedirs(self.rdir)
        os.makedirs(self.sdir)
        self.s.update({"replay_dirs": [self.rdir], "shot_dirs": [self.sdir]})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _text(self):
        return "\n".join(self.log.lines)

    def test_reports_version_and_folder_counts(self):
        self.assertTrue(cli._preflight(self.s, FakeClient(), self.log))
        t = self._text()
        self.assertIn("Debrief Uploader", t)
        self.assertIn("1 replay folder(s) and 1 screenshot folder(s)", t)
        self.assertIn("signed in as a1@example.com", t)

    def test_no_screenshot_folder_is_an_error_not_silence(self):
        self.s["shot_dirs"] = []
        cli._preflight(self.s, FakeClient(), self.log)
        self.assertIn("no screenshot folder", self._text())

    def test_missing_folder_is_named(self):
        self.s["shot_dirs"] = [os.path.join(self.tmp, "gone")]
        cli._preflight(self.s, FakeClient(), self.log)
        self.assertIn("folder is missing", self._text())

    def test_signed_out_is_an_error(self):
        cli._preflight(self.s, FakeClient(signed_in=False), self.log)
        self.assertIn("not signed in", self._text())

    def test_no_replay_folder_stops_startup(self):
        self.s["replay_dirs"] = []
        self.assertFalse(cli._preflight(self.s, FakeClient(), self.log))
        self.assertIn("no replay folder", self._text())


class TestTrayStartupReportsTheSame(unittest.TestCase):
    """The regression: `run --tray` must not skip preflight."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["GD_UPLOADER_HOME"] = self.tmp
        rdir = os.path.join(self.tmp, "replays")
        os.makedirs(rdir)
        s = config.Settings()
        s.update({"replay_dirs": [rdir], "shot_dirs": [], "_dirs_pinned": True})
        s.save()

    def tearDown(self):
        os.environ.pop("GD_UPLOADER_HOME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tray_path_runs_preflight_first(self):
        from debrief_uploader import tray
        called = {}
        real_run = tray.run

        def fake_run(settings, store, client, log):
            called["lines"] = list(log.lines)
            return 0

        tray.run = fake_run
        try:
            class Args:
                tray = True
                once = False
                quiet = True
            cli.cmd_run(Args())
        finally:
            tray.run = real_run

        text = "\n".join(called.get("lines", []))
        self.assertIn("Debrief Uploader", text,
                      "the tray path must stamp the version")
        self.assertIn("replay folder(s)", text,
                      "the tray path must report what it is watching")
        self.assertIn("no screenshot folder", text,
                      "the tray path must warn that scorecards can never match")


if __name__ == "__main__":
    unittest.main(verbosity=2)
