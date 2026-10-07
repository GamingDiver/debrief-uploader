"""What must never be read or sent: excluded folders, files from before the
user started watching, and anything before the first-run folder check.

Prompted by a tester (2026-10-07): sensitive replays can live on the same PC
and must never leave it by accident.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from debrief_uploader import config


class TestExclusionMatching(unittest.TestCase):

    def test_inside_excluded_folder(self):
        ex = [os.path.join("a", "st")]
        self.assertTrue(config.is_excluded(os.path.join("a", "st", "x.wowsreplay"), ex))
        self.assertTrue(config.is_excluded(os.path.join("a", "st", "d", "x"), ex))
        self.assertTrue(config.is_excluded(os.path.join("a", "st"), ex))

    def test_sibling_with_shared_prefix_is_not_excluded(self):
        ex = [os.path.join("a", "st")]
        self.assertFalse(config.is_excluded(os.path.join("a", "stable", "x"), ex))
        self.assertFalse(config.is_excluded(os.path.join("a", "x"), ex))

    def test_no_exclusions(self):
        self.assertFalse(config.is_excluded("anything", []))
        self.assertFalse(config.is_excluded("anything", None))

    @unittest.skipUnless(os.name == "nt", "Windows paths are case-insensitive")
    def test_case_insensitive_on_windows(self):
        self.assertTrue(config.is_excluded(r"C:\Games\ST\a.wowsreplay", [r"c:\games\st"]))


class TestDedupe(unittest.TestCase):

    def test_same_folder_twice(self):
        d = os.path.join("x", "replays")
        self.assertEqual(config.dedupe_dirs([d, d + os.sep, d]), [d])

    @unittest.skipUnless(os.name == "nt", "Windows paths are case-insensitive")
    def test_registry_and_fallback_spellings_collapse(self):
        """A tester's screenshot: the same replays folder listed in
        lowercase (Steam registry) and title case (fallback list)."""
        a = r"c:\program files (x86)\steam\steamapps\common\World of Warships Legends\replays"
        b = r"C:\Program Files (x86)\Steam\steamapps\common\World of Warships Legends\replays"
        self.assertEqual(len(config.dedupe_dirs([a, b])), 1)

    def test_settings_dedupe_on_load(self):
        d = os.path.join("x", "replays")
        s = config.Settings({"replay_dirs": [d, d]})
        self.assertEqual(s["replay_dirs"], [d])


class TestSteamAppId(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.apps = os.path.join(self.root, "steamapps")
        os.makedirs(self.apps)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def manifest(self, appid, installdir):
        with open(os.path.join(self.apps, "appmanifest_%s.acf" % appid), "w") as f:
            f.write('"AppState"\n{\n\t"appid"\t\t"%s"\n\t"installdir"\t\t"%s"\n}\n'
                    % (appid, installdir))

    def test_reads_appid_from_manifest(self):
        self.manifest("2964090", "World of Warships Legends")
        self.manifest("1623730", "Palworld")
        self.manifest("999", "World of Warships Legends Other")
        self.assertEqual(config._wowsl_appids(self.root), {"2964090"})

    def test_falls_back_to_known_appid(self):
        self.manifest("1623730", "Palworld")
        self.assertEqual(config._wowsl_appids(self.root), {config.WOWSL_STEAM_APPID})


class TestFirstRunFlag(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["GD_UPLOADER_HOME"] = self.home

    def tearDown(self):
        os.environ.pop("GD_UPLOADER_HOME", None)
        shutil.rmtree(self.home, ignore_errors=True)

    def test_new_install_is_not_watching(self):
        s = config.Settings.load()
        self.assertFalse(s["watching_confirmed"])
        self.assertIsNone(s["watching_since"])

    def test_existing_install_keeps_watching_from_now(self):
        with open(config.settings_path(), "w") as f:
            json.dump({"replay_dirs": ["r"], "shot_dirs": ["s"]}, f)
        s = config.Settings.load()
        self.assertTrue(s["watching_confirmed"])
        self.assertIsNotNone(s["watching_since"])

    def test_old_1000kb_floor_is_migrated(self):
        """A tester's 500-700 KB JPEG scorecards were all skipped (2026-10-07)."""
        with open(config.settings_path(), "w") as f:
            json.dump({"shot_min_kb": 1000}, f)
        self.assertEqual(config.Settings.load()["shot_min_kb"], 100)

    def test_a_chosen_floor_is_kept(self):
        with open(config.settings_path(), "w") as f:
            json.dump({"shot_min_kb": 200}, f)
        self.assertEqual(config.Settings.load()["shot_min_kb"], 200)

    def test_confirm_sets_cutoff_once(self):
        s = config.Settings.load()
        s.confirm_watching(now=1000.0)
        s.confirm_watching(now=2000.0)
        again = config.Settings.load()
        self.assertTrue(again["watching_confirmed"])
        self.assertEqual(again["watching_since"], 1000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
