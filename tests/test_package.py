"""The package itself must be importable and complete.

v1.0.8 shipped with an empty __init__.py: a version-bump one-liner of the
form open(p, "w").write(open(p).read()...) truncated the file before reading
it, and the tests were run BEFORE that edit, so nothing caught it. The app
died on startup with "module 'debrief_uploader' has no attribute
'__version__'".

Two lessons, both encoded here: assert the package's own surface, and drive
the CLI as a real subprocess so an import-time break cannot pass.
"""
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)


class TestPackageSurface(unittest.TestCase):

    def test_version_exists_and_is_sane(self):
        import debrief_uploader
        self.assertTrue(hasattr(debrief_uploader, "__version__"),
                        "startup reads this; an empty __init__ kills the app")
        self.assertRegex(debrief_uploader.__version__, r"^\d+\.\d+\.\d+$")

    def test_init_is_not_empty(self):
        p = os.path.join(ROOT, "debrief_uploader", "__init__.py")
        self.assertGreater(os.path.getsize(p), 0)

    def test_every_module_imports(self):
        import importlib
        for name in ("api", "cli", "config", "engine", "images", "log",
                     "matcher", "oauth", "state", "tray", "uploader",
                     "watcher"):
            importlib.import_module("debrief_uploader." + name)


class TestCliRunsAsASubprocess(unittest.TestCase):
    """Import-time breakage only shows up when something actually starts."""

    def _run(self, *args, **kw):
        env = dict(os.environ)
        env["GD_UPLOADER_HOME"] = kw.get("home", os.path.join(ROOT, ".tmp-home"))
        return subprocess.run([sys.executable, "-m", "debrief_uploader"] + list(args),
                              cwd=ROOT, capture_output=True, text=True,
                              timeout=60, env=env)

    def test_help_works(self):
        r = self._run("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("debrief-uploader", r.stdout)

    def test_installer_cleans_up_the_same_startup_entry(self):
        """installer.iss deletes the Run value autostart.py writes, by name."""
        from debrief_uploader import autostart
        with open(os.path.join(ROOT, "installer.iss"), encoding="utf-8") as f:
            self.assertIn("RunName = '%s';" % autostart.NAME, f.read())

    def test_status_works(self):
        import shutil
        home = os.path.join(ROOT, ".tmp-home-status")
        shutil.rmtree(home, ignore_errors=True)
        try:
            r = self._run("status", home=home)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Account", r.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_doctor_works_and_reports_the_version(self):
        import shutil
        home = os.path.join(ROOT, ".tmp-home-doctor")
        shutil.rmtree(home, ignore_errors=True)
        try:
            r = self._run("doctor", home=home)
            self.assertEqual(r.returncode, 0, r.stderr)
            import debrief_uploader
            self.assertIn(debrief_uploader.__version__, r.stdout)
            self.assertIn("Tray: ", r.stdout)     # release CI reads this on the exe
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_run_once_starts_and_reports_preflight(self):
        """The exact path that crashed: cmd_run -> _preflight -> __version__."""
        import shutil
        home = os.path.join(ROOT, ".tmp-home-run")
        shutil.rmtree(home, ignore_errors=True)
        os.makedirs(os.path.join(home, "replays"))
        try:
            self._run("setup", "--replay-dir", os.path.join(home, "replays"),
                      "--shot-dir", os.path.join(home, "replays"), home=home)
            r = self._run("run", "--once", home=home)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Debrief Uploader", r.stdout + r.stderr)
            self.assertNotIn("Traceback", r.stdout + r.stderr)
        finally:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
