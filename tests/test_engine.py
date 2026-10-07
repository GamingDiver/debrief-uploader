"""End-to-end engine behaviour against a fake site.

Covers the integration cases from the spec (section 13, tests 11-15): the
four-step upload order, the duplicate pre-check, a terminal gate parking the
queue, a network failure resuming, and a crash between create_replay and the
screenshots never producing a second row.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image

from debrief_uploader import config, engine, matcher, watcher
from debrief_uploader.api import ApiError, Session
from debrief_uploader.log import Log
from debrief_uploader.state import Store


class FakeClient:
    """A stand-in for Supabase that enforces the real ordering rules."""

    def __init__(self):
        self.session = Session(access="a", refresh="r",
                               expires_at=time.time() + 3600, user_id="uid-1")
        self.calls = []
        self.rows = {}              # replay_id -> {short_code, shots:[]}
        self.raw = {}
        self.existing = None
        self.created_visibility = "__unset__"
        self.fail_next = None       # ApiError to raise on the next storage/rpc
        self.fail_on = None         # which call to fail: 'meta','raw','rpc','shot'
        self.meta = {
            "mapName": "spaces/38_Canada", "matchGroup": "pvp",
            "playerName": "a1", "playerVehicle": "PJSD219-Kitakaze",
            "duration": 900, "dateTime": "28.08.2026 13:53:40",
        }
        self._n = 0

    def _maybe_fail(self, which):
        if self.fail_on == which and self.fail_next:
            e, self.fail_next, self.fail_on = self.fail_next, None, None
            raise e

    def ensure_auth(self):
        pass

    def replay_meta(self, raw):
        self.calls.append("meta")
        self._maybe_fail("meta")
        return dict(self.meta)

    def find_existing(self, player, played_at):
        self.calls.append("find")
        return self.existing

    def storage_upload(self, bucket, path, data, content_type, upsert=False):
        if bucket == "replays":
            self.calls.append("raw")
            self._maybe_fail("raw")
            self.raw[path] = data
        else:
            self.calls.append("shot")
            self._maybe_fail("shot")
            rid = path.split("/")[0]
            if rid not in self.rows:
                raise ApiError("policy", "row must exist before screenshots")
            self.rows[rid]["shots"].append(path)
        return True

    def rpc(self, fn, params=None):
        self.calls.append("rpc:" + fn)
        self._maybe_fail("rpc")
        if fn == "create_replay":
            self.created_visibility = (params or {}).get("p_visibility")
            self._n += 1
            rid = "rid-%d" % self._n
            self.rows[rid] = {"short_code": "code%d" % self._n, "shots": []}
            return {"id": rid, "short_code": "code%d" % self._n,
                    "visibility": "public"}
        return True


class EngineCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rdir = os.path.join(self.tmp, "replays")
        self.sdir = os.path.join(self.tmp, "shots")
        self.staging = os.path.join(self.tmp, "staging")
        os.makedirs(self.rdir)
        os.makedirs(self.sdir)
        self._orig_staging = config.staging_dir
        config.staging_dir = lambda: self.staging

        self.s = config.Settings()
        self.s.update({"replay_dirs": [self.rdir], "shot_dirs": [self.sdir],
                       "settle_quiet": 0.01, "settle_sleep": 0.01,
                       "min_upload_gap": 0, "max_uploads_per_hour": 0,
                       "pair_settle": 0.05,
                       # these fixtures are flat-colour and tiny; the size and
                       # age filters get their own tests below
                       "shot_min_kb": 0, "shot_max_age_hours": 24 * 365,
                       "watching_confirmed": True})
        self.store = Store(os.path.join(self.tmp, "state.db"))
        self.client = FakeClient()
        self.log = Log(None, echo=False)
        self.eng = engine.Engine(self.s, self.store, self.client, self.log)
        self.eng._first_scan = False        # treat everything as live
        self.eng._watch_start = lambda path: 0

    def tearDown(self):
        config.staging_dir = self._orig_staging
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- fixtures ---------------------------------------------------------
    def make_replay(self, name, mtime=None, size=4096):
        p = os.path.join(self.rdir, name)
        with open(p, "wb") as f:
            f.write(watcher.MAGIC + os.urandom(size))
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def make_shot(self, name, mtime=None):
        p = os.path.join(self.sdir, name)
        Image.new("RGB", (1920, 1080), (20, 30, 40)).save(p)
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def battle(self, tag, when, with_shots=True):
        self.make_replay("2026_%s.wowsreplay" % tag, mtime=when)
        if with_shots:
            self.make_shot("shot_%s_a.png" % tag, mtime=when + 40)
            self.make_shot("shot_%s_b.png" % tag, mtime=when + 47)


class TestUploadSequence(EngineCase):

    def test_11_full_four_step_upload_in_order(self):
        self.battle("A", time.time() - 100)
        for _ in range(4):
            self.eng.tick()
        row = self.store.replays()[0]
        self.assertEqual(row["state"], matcher.UPLOADED)
        self.assertEqual(row["short_code"], "code1")
        seq = [c for c in self.client.calls
               if c in ("meta", "raw", "rpc:create_replay", "shot")]
        self.assertEqual(seq[:3], ["meta", "raw", "rpc:create_replay"],
                         "the row must exist before any screenshot")
        self.assertEqual(seq.count("shot"), 2)
        self.assertEqual(sorted(self.client.rows["rid-1"]["shots"]),
                         ["rid-1/shot1.jpg", "rid-1/shot2.jpg"])

    def test_replay_is_quarantined_before_anything_else(self):
        p = self.make_replay("q.wowsreplay", mtime=time.time() - 60)
        self.eng.intake_replays()
        staged = self.store.replays()[0]["staged_path"]
        self.assertTrue(os.path.exists(staged))
        os.remove(p)                       # the game rotates it away
        for _ in range(4):
            self.eng.tick()
        self.assertTrue(os.path.exists(staged), "quarantine must survive rotation")

    def test_12_duplicate_attaches_shots_instead_of_new_row(self):
        self.client.existing = {"id": "rid-existing", "short_code": "zz9999",
                                "status": "ready"}
        self.client.rows["rid-existing"] = {"short_code": "zz9999", "shots": []}
        self.battle("B", time.time() - 100)
        for _ in range(4):
            self.eng.tick()
        self.assertNotIn("rpc:create_replay", self.client.calls)
        self.assertIn("rpc:reprocess_own_replay", self.client.calls)
        self.assertEqual(self.store.replays()[0]["short_code"], "zz9999")
        self.assertEqual(len(self.client.rows["rid-existing"]["shots"]), 2)

    def test_13_supporter_gate_parks_the_queue(self):
        self.battle("C", time.time() - 100)
        self.client.fail_on = "rpc"
        self.client.fail_next = ApiError("feature_supporter_early_access",
                                         "supporter early access", terminal=True)
        for _ in range(4):
            self.eng.tick()
        self.assertIsNotNone(self.eng.blocked)
        before = len(self.client.calls)
        self.eng.tick()
        self.assertEqual(len(self.client.calls), before,
                         "a parked queue must not keep retrying")

    def test_14_network_failure_retries_and_completes(self):
        self.battle("D", time.time() - 100)
        self.client.fail_on = "raw"
        self.client.fail_next = ApiError("network", "connection reset")
        for _ in range(4):
            self.eng.tick()
        row = self.store.replays()[0]
        self.assertEqual(row["state"], matcher.READY)
        self.assertGreater(row["attempts"], 0)
        self.assertGreater(row["next_try"], time.time())
        self.store.update_replay(row["md5"], next_try=0)     # backoff elapses
        self.eng.tick()
        self.assertEqual(self.store.replays()[0]["state"], matcher.UPLOADED)
        self.assertEqual(len(self.client.rows), 1, "exactly one row")

    def test_15_crash_after_row_creation_does_not_double_upload(self):
        self.battle("E", time.time() - 100)
        self.client.fail_on = "shot"
        self.client.fail_next = ApiError("network", "dropped")
        for _ in range(4):
            self.eng.tick()
        row = self.store.replays()[0]
        self.assertEqual(row["remote_id"], "rid-1")
        self.assertEqual(row["state"], matcher.UPLOADING)
        self.store.update_replay(row["md5"], next_try=0)
        self.eng.tick()
        self.assertEqual(self.store.replays()[0]["state"], matcher.UPLOADED)
        self.assertEqual(len(self.client.rows), 1,
                         "must never call create_replay twice")
        self.assertEqual(self.client.calls.count("rpc:create_replay"), 1)


class TestEngineMatching(EngineCase):

    def test_training_room_uploads_with_no_screenshots(self):
        self.client.meta = dict(self.client.meta, matchGroup="training_room")
        self.make_replay("t.wowsreplay", mtime=time.time() - 100)
        for _ in range(4):
            self.eng.tick()
        row = self.store.replays()[0]
        self.assertEqual(row["state"], matcher.UPLOADED)
        self.assertEqual(self.client.rows["rid-1"]["shots"], [])

    def test_back_to_back_battles_each_get_their_own_shots(self):
        now = time.time()
        self.battle("A", now - 900)
        self.battle("B", now - 600)
        for _ in range(8):
            self.eng.tick()
        rows = {r["filename"]: r for r in self.store.replays()}
        for name, row in rows.items():
            self.assertEqual(row["state"], matcher.UPLOADED, name)
            self.assertEqual(len(self.store.shots_for(row["md5"])), 2, name)
        self.assertEqual(len(self.client.rows), 2)

    def test_skipped_battle_is_held_not_guessed(self):
        now = time.time()
        # both under the 15-minute no-scorecard upload age: this is about the
        # ambiguity, not about A eventually going up without scorecards
        self.make_replay("2026_A.wowsreplay", mtime=now - 700)   # no shots
        self.battle("B", now - 400)
        for _ in range(6):
            self.eng.tick()
        states = sorted(r["state"] for r in self.store.replays())
        self.assertIn(matcher.HELD, states, "must ask rather than guess")
        self.assertEqual(len(self.client.rows), 0, "nothing uploaded while unsure")

    def test_confirming_a_held_item_uploads_it(self):
        now = time.time()
        self.make_replay("2026_A.wowsreplay", mtime=now - 900)
        self.battle("B", now - 600)
        for _ in range(6):
            self.eng.tick()
        held = [r for r in self.store.replays() if r["state"] == matcher.HELD]
        self.assertEqual(len(held), 1)
        self.eng.confirm(held[0]["md5"])
        for _ in range(3):
            self.eng.tick()
        self.assertEqual(self.store.replay(held[0]["md5"])["state"],
                         matcher.UPLOADED)

    def test_reassigning_moves_the_shots(self):
        now = time.time()
        self.make_replay("2026_A.wowsreplay", mtime=now - 900)
        self.battle("B", now - 600)
        for _ in range(6):
            self.eng.tick()
        rows = {r["filename"]: r for r in self.store.replays()}
        a, b = rows["2026_A.wowsreplay"], rows["2026_B.wowsreplay"]
        self.eng.reassign(b["md5"], a["md5"])
        self.assertEqual(len(self.store.shots_for(a["md5"])), 2)
        self.assertEqual(len(self.store.shots_for(b["md5"])), 0)

    def test_orphan_keeps_the_file_and_uploads_nothing_when_disabled(self):
        self.s["upload_without_scorecards"] = False
        old = time.time() - 3 * 3600
        self.make_replay("old.wowsreplay", mtime=old)
        for _ in range(3):
            self.eng.tick()
        row = self.store.replays()[0]
        self.assertEqual(row["state"], matcher.ORPHAN)
        self.assertTrue(os.path.exists(row["staged_path"]))
        self.assertEqual(len(self.client.rows), 0)

    def test_replay_without_scorecards_uploads_after_15_minutes(self):
        # Greg 2026-09-26: any replay at least 15 minutes old goes up,
        # screenshots or not (the default).
        self.make_replay("lone.wowsreplay", mtime=time.time() - 16 * 60)
        for _ in range(4):
            self.eng.tick()
        row = self.store.replays()[0]
        self.assertEqual(row["state"], matcher.UPLOADED)
        self.assertEqual(len(self.client.rows), 1)

    def test_replay_without_scorecards_waits_until_15_minutes(self):
        self.make_replay("fresh.wowsreplay", mtime=time.time() - 5 * 60)
        for _ in range(4):
            self.eng.tick()
        row = self.store.replays()[0]
        self.assertNotEqual(row["state"], matcher.UPLOADED)
        self.assertEqual(len(self.client.rows), 0)

    def test_min_age_holds_an_early_orphan(self):
        # two later battles orphan a replay early; it still waits for 15 min
        now = time.time()
        for i, age in enumerate((12, 8, 4)):
            self.make_replay("r%d.wowsreplay" % i, mtime=now - age * 60)
        for _ in range(8):
            self.eng.tick()
        self.assertEqual(len(self.client.rows), 0)

    def test_non_screenshot_images_are_ignored(self):  # noqa: D401
        self.battle("A", time.time() - 100)
        Image.new("RGB", (64, 64), (0, 0, 0)).save(os.path.join(self.sdir, "icon.png"))
        for _ in range(4):
            self.eng.tick()
        row = self.store.replays()[0]
        self.assertEqual(len(self.store.shots_for(row["md5"])), 2,
                         "an icon must not be uploaded as a scorecard")

    def test_backfill_is_held_not_auto_uploaded(self):
        del self.eng._watch_start          # the real per-folder clock
        self.battle("Z", time.time() - 3600)
        for _ in range(6):
            self.eng.tick()
        row = self.store.replays()[0]
        self.assertEqual(row["backfill"], 1)
        self.assertEqual(row["state"], matcher.HELD)
        self.assertEqual(len(self.client.rows), 0)
        self.assertNotIn("meta", self.client.calls,
                         "nothing about a backfilled replay leaves the PC "
                         "until the user confirms it")

    def test_backfill_without_scorecards_is_never_auto_uploaded(self):
        """The leak found 2026-10-07: replays already in the folder at first
        launch became 'no scorecards' orphans and uploaded unasked."""
        del self.eng._watch_start
        now = time.time()
        for i in range(5):
            self.make_replay("old%d.wowsreplay" % i, mtime=now - (2 + i) * 3600)
        for _ in range(12):
            self.eng.tick(now + 3 * 3600)
        self.assertEqual(self.client.calls.count("raw"), 0)
        self.assertEqual(self.client.calls.count("meta"), 0)
        self.assertEqual({r["state"] for r in self.store.replays()},
                         {matcher.HELD})

    def test_confirmed_backfill_uploads(self):
        del self.eng._watch_start
        self.make_replay("old.wowsreplay", mtime=time.time() - 3 * 3600)
        self.eng.tick()
        row = self.store.replays()[0]
        self.eng.confirm(row["md5"])
        for _ in range(3):
            self.eng.tick()
        self.assertEqual(self.store.replay(row["md5"])["state"], matcher.UPLOADED)

    def test_review_mode_holds_no_scorecard_uploads_too(self):
        self.s["review_mode"] = True
        now = time.time()
        self.make_replay("r.wowsreplay", mtime=now - 20 * 60)
        for _ in range(6):
            self.eng.tick(now)
        self.assertEqual(self.client.calls.count("raw"), 0)
        self.assertEqual(self.store.replays()[0]["state"], matcher.HELD)

    def test_nothing_is_read_until_folders_are_confirmed(self):
        self.s["watching_confirmed"] = False
        self.battle("U", time.time() - 100)
        for _ in range(4):
            self.eng.tick()
        self.assertEqual(self.store.replays(), [])
        self.assertEqual(self.client.calls, [])
        self.assertFalse(os.path.exists(self.staging),
                         "not even a staged copy before the user says go")

    def test_files_older_than_first_run_are_never_read(self):
        now = time.time()
        self.s["watching_since"] = now - 600
        self.make_replay("before.wowsreplay", mtime=now - 900)
        self.make_shot("before.png", mtime=now - 890)
        self.battle("after", now - 100)
        for _ in range(4):
            self.eng.tick()
        names = [r["filename"] for r in self.store.replays()]
        self.assertEqual(names, ["2026_after.wowsreplay"])
        self.assertFalse(self.store.have_shot(os.path.join(self.sdir, "before.png")))

    def test_battle_played_while_paused_is_held_on_resume(self):
        now = time.time()
        self.eng.pause(now - 600)
        self.battle("P", now - 300)
        self.eng.resume(now - 60)
        for _ in range(6):
            self.eng.tick(now + 3600)
        row = self.store.replays()[0]
        self.assertEqual(row["state"], matcher.HELD)
        self.assertEqual(self.client.calls.count("raw"), 0)
        self.assertEqual(self.store.shots_for(row["md5"]), [],
                         "screenshots taken while paused are not read")

    def test_excluded_folder_is_never_read(self):
        st = os.path.join(self.rdir, "sensitive")
        os.makedirs(st)
        with open(os.path.join(st, "private.wowsreplay"), "wb") as f:
            f.write(watcher.MAGIC + os.urandom(64))
        self.s["replay_dirs"].append(st)
        self.s["exclude_dirs"] = [st.upper() if os.name == "nt" else st]
        for _ in range(4):
            self.eng.tick()
        self.assertEqual(self.store.replays(), [])

    def test_excluding_a_folder_later_drops_what_was_not_uploaded(self):
        self.s["review_mode"] = True
        self.battle("X", time.time() - 100)
        for _ in range(4):
            self.eng.tick()
        row = self.store.replays()[0]
        self.assertEqual(row["state"], matcher.HELD)
        staged = row["staged_path"]
        self.s["exclude_dirs"] = [self.rdir, self.sdir]
        self.assertEqual(self.eng.apply_exclusions(), 1)
        row = self.store.replay(row["md5"])
        self.assertEqual(row["state"], matcher.SKIPPED)
        self.assertFalse(os.path.exists(staged))
        self.assertEqual(self.store.shots_for(row["md5"]), [])
        self.assertEqual(self.client.calls.count("raw"), 0)

    def test_temp_replay_is_never_picked_up(self):
        with open(os.path.join(self.rdir, watcher.TEMP_NAME), "wb") as f:
            f.write(watcher.MAGIC + b"\x00" * 100)
        self.eng.tick()
        self.assertEqual(len(self.store.replays()), 0)

    def test_non_replay_file_is_ignored(self):
        with open(os.path.join(self.rdir, "junk.wowsreplay"), "wb") as f:
            f.write(b"NOPE" + b"\x00" * 100)
        self.eng.tick()
        self.assertEqual(len(self.store.replays()), 0)


class TestPacer(unittest.TestCase):

    def test_gap_and_hourly_ceiling(self):
        from debrief_uploader.uploader import Pacer
        p = Pacer(min_gap=2, per_hour=3)
        t = 1000.0
        self.assertEqual(p.wait_needed(t), 0)
        p.note(t)
        self.assertAlmostEqual(p.wait_needed(t + 0.5), 1.5)
        self.assertEqual(p.wait_needed(t + 3), 0)
        p.note(t + 3)
        p.note(t + 6)
        self.assertGreater(p.wait_needed(t + 9), 3000, "hourly ceiling holds")
        self.assertEqual(p.wait_needed(t + 3700), 0, "ceiling releases")

    def test_backoff_grows_and_caps(self):
        from debrief_uploader.uploader import backoff, RETRY_MAX
        self.assertEqual(backoff(1), 30)
        self.assertEqual(backoff(2), 60)
        self.assertEqual(backoff(3), 120)
        self.assertEqual(backoff(20), RETRY_MAX)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDiagnostics(EngineCase):
    """`doctor` is what makes this debuggable on someone else's PC, so it has
    to survive every state -- including the empty one. A NameError in here
    would land as 'unexpected: ...' in a log and help nobody."""

    def test_diagnose_on_an_empty_store(self):
        lines = self.eng.diagnose()
        self.assertTrue(any("ACCOUNT" in l for l in lines))
        self.assertTrue(any("FOLDERS" in l for l in lines))

    def test_diagnose_with_replays_and_shots(self):
        self.battle("A", time.time() - 100)
        self.eng.tick()
        lines = "\n".join(self.eng.diagnose())
        self.assertIn("SEEN", lines)
        self.assertIn("WHAT THE MATCHER WOULD DO RIGHT NOW", lines)

    def test_diagnose_after_a_full_upload(self):
        self.battle("A", time.time() - 100)
        for _ in range(4):
            self.eng.tick()
        lines = "\n".join(self.eng.diagnose())
        self.assertIn("uploaded", lines)

    def test_diagnose_reports_a_held_item(self):
        now = time.time()
        self.make_replay("2026_A.wowsreplay", mtime=now - 900)
        self.battle("B", now - 600)
        for _ in range(6):
            self.eng.tick()
        lines = "\n".join(self.eng.diagnose())
        self.assertIn("held", lines)

    def test_counts_covers_every_state(self):
        c = self.eng.counts()
        for k in (matcher.NEW, matcher.AWAITING_SHOTS, matcher.HELD,
                  matcher.READY, matcher.UPLOADING, matcher.UPLOADED,
                  matcher.ORPHAN, matcher.SKIPPED):
            self.assertIn(k, c)

    def test_signed_out_says_so_once(self):
        class NoAuth(FakeClient):
            def __init__(self):
                super().__init__()
                self.session.access = None
                self.session.refresh = None
        self.eng.client = NoAuth()
        self.battle("A", time.time() - 100)
        for _ in range(3):
            self.eng.tick()
        said = [l for l in self.log.lines if "not signed in" in l]
        self.assertEqual(len(said), 1, "must warn, but only once")

    def test_heartbeat_reports_what_is_held(self):
        self.battle("A", time.time() - 100)
        self.eng._last_beat = 0
        self.eng.tick()
        self.eng._last_beat = 0
        self.eng.HEARTBEAT = 0
        self.eng.tick()
        self.assertTrue(any("holding:" in l or "Ready for review" in l
                            for l in self.log.lines))


class TestShotScanIsBounded(EngineCase):
    """A screenshots folder is a lifetime archive.

    The first version settle-checked and decoded every image in it, oldest
    first, at ~2 s each. On a real machine with thousands of files it spent a
    whole night on last month and never reached that evening's battle. These
    tests pin the filters that make the scan cheap and current.
    """

    def setUp(self):
        super().setUp()
        self.s.update({"shot_min_kb": 1000, "shot_max_age_hours": 4})

    W, H = 1000, 700          # screenshot-shaped: clears the dimension check

    def _shot(self, name, when=None, big=True):
        """A screenshot-shaped PNG.

        `big` fills it with noise so it lands in the megabytes like a real
        scorecard; otherwise flat colour, which compresses to a few KB and
        stands in for the UI grabs and thumbnails the size floor exists to
        skip.
        """
        from PIL import Image
        if big:
            im = Image.frombytes("RGB", (self.W, self.H),
                                 os.urandom(self.W * self.H * 3))
        else:
            im = Image.new("RGB", (self.W, self.H), (20, 30, 40))
        p = os.path.join(self.sdir, name)
        im.save(p)
        if when:
            os.utime(p, (when, when))
        return p

    def test_old_screenshots_are_never_opened(self):
        old = time.time() - 30 * 3600
        self._shot("Screenshot 2026-08-02 180748.png", when=old)
        self.eng.intake_shots()
        self.assertEqual(len(self.store.free_shots()), 0,
                         "anything older than the window must not be read")

    def test_recent_large_screenshot_is_taken(self):
        self._shot("Screenshot recent.png", when=time.time() - 60)
        self.eng.intake_shots()
        self.assertEqual(len(self.store.free_shots()), 1)

    def test_small_files_are_skipped_without_being_opened(self):
        self._shot("small.png", when=time.time() - 60, big=False)
        self.eng.intake_shots()
        self.assertEqual(len(self.store.free_shots()), 0)

    def test_scan_is_capped_per_pass(self):
        self.s["shot_scan_limit"] = 3
        now = time.time()
        for i in range(8):
            self._shot("shot%d.png" % i, when=now - 100 - i)
        self.assertEqual(self.eng.intake_shots(), 3,
                         "one pass must never walk the whole folder")

    def test_newest_are_taken_first(self):
        self.s["shot_scan_limit"] = 2
        now = time.time()
        for i in range(6):
            self._shot("shot%d.png" % i, when=now - 60 - i * 60)
        self.eng.intake_shots()
        taken = sorted(os.path.basename(r["path"]) for r in self.store.free_shots())
        self.assertEqual(taken, ["shot0.png", "shot1.png"],
                         "the battle that just finished must not queue behind "
                         "last month")

    def test_a_stale_backlog_is_forgotten_on_start(self):
        from debrief_uploader.engine import Engine
        old = time.time() - 40 * 3600
        for i in range(5):
            self.store.add_shot("/old/shot%d.png" % i, old)
        self.assertEqual(len(self.store.free_shots()), 5)
        Engine(self.s, self.store, self.client, self.log)   # boot-time prune
        self.assertEqual(len(self.store.free_shots()), 0)

    def test_ignored_files_are_not_re_read_every_pass(self):
        from PIL import Image
        p = os.path.join(self.sdir, "wide-but-short.png")
        Image.frombytes("RGB", (1400, 300), os.urandom(1400 * 300 * 3)).save(p)
        os.utime(p, (time.time() - 60,) * 2)
        self.eng.intake_shots()
        first = len(self.log.lines)
        self.eng.intake_shots()
        self.assertEqual(len(self.log.lines), first,
                         "a file judged once must stay judged")


class TestDoctorTellsYouWhatToDo(EngineCase):
    """A count is not an instruction.

    The app reported "5 need you" for hours while the user waited for an
    upload that was never going to happen without them. Every state that
    blocks progress must name the command that clears it.
    """

    def test_held_items_name_the_review_command(self):
        now = time.time()
        self.make_replay("2026_A.wowsreplay", mtime=now - 900)
        self.battle("B", now - 600)
        for _ in range(6):
            self.eng.tick()
        text = "\n".join(self.eng.diagnose())
        self.assertIn("WHAT TO DO NEXT", text)
        self.assertIn("Debrief.cmd review", text)
        self.assertIn("will\nNOT upload".replace("\n", " "),
                      text.replace("\n", " "))

    def test_log_says_how_to_clear_a_hold(self):
        now = time.time()
        self.make_replay("2026_A.wowsreplay", mtime=now - 900)
        self.battle("B", now - 600)
        for _ in range(6):
            self.eng.tick()
        self.assertTrue(any("Debrief.cmd review" in l for l in self.log.lines),
                        "the log must say how to unblock, not just that it is blocked")

    def test_signed_out_names_the_login_command(self):
        self.eng.client.session.access = None
        self.eng.client.session.refresh = None
        text = "\n".join(self.eng.diagnose())
        self.assertIn("Debrief.cmd login", text)

    def test_uploaded_explains_why_it_might_be_invisible(self):
        self.battle("A", time.time() - 100)
        for _ in range(4):
            self.eng.tick()
        text = "\n".join(self.eng.diagnose())
        self.assertIn("uploaded", text)
        self.assertIn("private", text)

    def test_clean_state_says_nothing_to_do(self):
        self.assertIn("Nothing to do", "\n".join(self.eng.diagnose()))


class TestHoldsAreRecheckedOnStart(EngineCase):
    """Holds created by the older, over-eager rule are released on start.

    Clearing up after a bug I shipped is not the user's job -- but a hold that
    is still genuinely ambiguous must survive, and a backfill hold is held for
    an entirely different reason and must not be touched.

    These set the database up directly: going through a full tick to reach a
    specific hold shape makes the assertions vague, and a test that cannot
    fail is worse than no test.
    """

    def _replay(self, md5, close_ago, started_ago, duration=900, state=None,
                backfill=0, note=None):
        now = time.time()
        self.store.add_replay(md5=md5, filename=md5 + ".wowsreplay",
                              staged_path="/staged/" + md5,
                              t_close=now - close_ago * 60,
                              header_dt=now - started_ago * 60,
                              duration_s=duration, match_group="pvp",
                              player_ship="PJSC008-Myoko", map_name="52_Britain",
                              state=state or matcher.AWAITING_SHOTS,
                              backfill=backfill, note=note)

    def _shots_on(self, md5, ago_min):
        now = time.time()
        for i in (1, 2):
            p = "/shots/%s-%d.png" % (md5, i)
            self.store.add_shot(p, now - ago_min * 60 + i)
            self.store.attach_shots([p], md5)

    def _boot(self):
        from debrief_uploader.engine import Engine
        return Engine(self.s, self.store, self.client, self.log)

    def test_the_reported_backlog_is_released(self):
        # A: started 30 min ago, 15-min limit -> ended by minute 15 at the
        # latest, i.e. 15 min before the screenshots. Not a rival.
        self._replay("A", close_ago=18, started_ago=30)
        self._replay("B", close_ago=1, started_ago=14, state=matcher.HELD,
                     note="an earlier battle is still missing its scorecards")
        self._shots_on("B", ago_min=0)
        self._boot()
        self.assertEqual(self.store.replay("B")["state"], matcher.READY,
                         "an older battle that had long since ended must not "
                         "hold this up")
        self.assertIsNone(self.store.replay("B")["note"])

    def test_a_real_ambiguity_survives(self):
        # A started 16 min ago and was left after 1 minute, but its 15-minute
        # battle ran on until minute 16 -- so its results appeared AFTER B
        # closed. That is the case the hold exists for.
        self._replay("A", close_ago=15, started_ago=16)
        self._replay("B", close_ago=2, started_ago=13, state=matcher.HELD,
                     note="an earlier battle is still missing its scorecards")
        self._shots_on("B", ago_min=0)
        self._boot()
        self.assertEqual(self.store.replay("B")["state"], matcher.HELD,
                         "a battle whose results could still be on screen is "
                         "a genuine rival")

    def test_backfill_holds_are_left_alone(self):
        self._replay("Z", close_ago=90, started_ago=105, state=matcher.HELD,
                     backfill=1, note="found before the app was running")
        self._shots_on("Z", ago_min=88)
        self._boot()
        self.assertEqual(self.store.replay("Z")["state"], matcher.HELD,
                         "a backfill hold is not an ambiguity hold")

    def test_a_hold_without_screenshots_is_not_released(self):
        self._replay("A", close_ago=40, started_ago=55)
        self._replay("B", close_ago=1, started_ago=14, state=matcher.HELD)
        self._boot()
        self.assertEqual(self.store.replay("B")["state"], matcher.HELD,
                         "nothing to upload, so nothing to release")

    def test_unknown_duration_keeps_the_hold(self):
        self._replay("A", close_ago=18, started_ago=30, duration=None)
        self._replay("B", close_ago=1, started_ago=14, state=matcher.HELD,
                     note="an earlier battle is still missing its scorecards")
        self._shots_on("B", ago_min=0)
        self._boot()
        self.assertEqual(self.store.replay("B")["state"], matcher.HELD,
                         "never resolve an ambiguity from a missing timestamp")

    def test_release_is_logged(self):
        self._replay("A", close_ago=18, started_ago=30)
        self._replay("B", close_ago=1, started_ago=14, state=matcher.HELD,
                     note="an earlier battle is still missing its scorecards")
        self._shots_on("B", ago_min=0)
        self._boot()
        self.assertTrue(any("no longer ambiguous" in l for l in self.log.lines))


class TestOverwritingExistingScreenshots(EngineCase):
    """Attaching screenshots to a replay that is already on the site.

    Storage has no UPDATE policy on these buckets -- only INSERT, SELECT and
    DELETE -- so `x-upsert` on an object that already exists is refused with
    "new row violates row-level security policy", even for its owner. Seen in
    the wild after a battle was uploaded from the website and the app then
    tried to attach its own copies of the scorecards.

    The delete-then-insert retry itself lives in api.Client.storage_upload and
    is tested in test_api.py; this covers what the engine does when the write
    genuinely cannot be made.
    """

    def test_a_permission_refusal_is_not_retried_forever(self):
        """If we can see a row but cannot write under it, upload our own copy
        rather than failing every thirty seconds all night."""
        class Unwritable(FakeClient):
            def storage_upload(self, bucket, path, data, content_type,
                               upsert=False):
                if bucket == "replays-data" and path.startswith("rid-web"):
                    e = ApiError("Unauthorized", "row-level security", 403)
                    e.permission = True
                    raise e
                return FakeClient.storage_upload(self, bucket, path, data,
                                                 content_type, upsert)

            def storage_delete(self, bucket, path):
                return False

        c = Unwritable()
        c.existing = {"id": "rid-web", "short_code": "5dc5a0", "status": "ready"}
        self.eng.client = c
        self.battle("A", time.time() - 100)
        for _ in range(5):
            self.eng.tick()
        row = self.store.replays()[0]
        self.assertEqual(row["state"], matcher.UPLOADED)
        self.assertNotEqual(row["short_code"], "5dc5a0",
                            "it should have uploaded its own copy")
        self.assertIn("rpc:create_replay", c.calls)


class TestUploadIsAnnouncedUsefully(EngineCase):
    """An uploaded battle is only finished from the app's point of view.

    What the user wants is the page, so the moment it is up they should be
    told it is ready and given the link -- not told that a file transfer
    completed.
    """

    def test_a_finished_upload_says_ready_for_review_with_the_link(self):
        self.battle("A", time.time() - 100)
        for _ in range(4):
            self.eng.tick()
        line = next((l for l in self.log.lines if "Ready for review" in l), "")
        self.assertTrue(line, "must announce readiness, not just completion")
        self.assertIn("/wowslegends/replays/?r=code1", line)

    def test_attaching_to_an_existing_battle_also_gives_the_link(self):
        self.client.existing = {"id": "rid-x", "short_code": "zz9999",
                                "status": "ready"}
        self.client.rows["rid-x"] = {"short_code": "zz9999", "shots": []}
        self.battle("B", time.time() - 100)
        for _ in range(4):
            self.eng.tick()
        self.assertTrue(any("Ready for review" in l and "zz9999" in l
                            for l in self.log.lines))


class TestTrainingRoomVisibility(EngineCase):
    """Training rooms upload private by default.

    They are practice, and unlike a real battle they carry no scorecard to
    anchor the Base XP research -- so there is nothing for the community to
    gain from them and a fair chance the uploader would rather they were not
    listed. It stays a setting, including an opt-out.
    """

    def _upload_training(self, **settings):
        self.s.update(settings)
        self.client.meta = dict(self.client.meta, matchGroup="training_room")
        self.make_replay("t.wowsreplay", mtime=time.time() - 100)
        for _ in range(4):
            self.eng.tick()
        return self.client.rpc_args

    def test_private_by_default(self):
        from debrief_uploader.uploader import visibility_for
        self.assertEqual(self.s["training_visibility"], "private")
        self.assertEqual(
            visibility_for({"match_group": "training_room"}, self.s), "private")

    def test_private_even_when_real_battles_are_public(self):
        from debrief_uploader.uploader import visibility_for
        self.s["visibility"] = "public"
        self.assertEqual(
            visibility_for({"match_group": "training_room"}, self.s), "private")

    def test_real_battles_are_untouched(self):
        from debrief_uploader.uploader import visibility_for
        self.s["visibility"] = "public"
        for grp in ("pvp", "cooperative", "ranked", "clan_elimination_cup", None):
            self.assertEqual(visibility_for({"match_group": grp}, self.s),
                             "public", grp)

    def test_the_setting_is_honoured(self):
        from debrief_uploader.uploader import visibility_for
        self.s["training_visibility"] = "fleet"
        self.assertEqual(
            visibility_for({"match_group": "training_room"}, self.s), "fleet")

    def test_empty_setting_means_treat_them_like_anything_else(self):
        from debrief_uploader.uploader import visibility_for
        self.s["visibility"] = "public"
        self.s["training_visibility"] = ""
        self.assertEqual(
            visibility_for({"match_group": "training_room"}, self.s), "public")

    def test_site_default_is_still_reachable(self):
        from debrief_uploader.uploader import visibility_for
        self.s["visibility"] = None
        self.s["training_visibility"] = ""
        self.assertIsNone(visibility_for({"match_group": "training_room"}, self.s))

    def test_case_is_not_load_bearing(self):
        from debrief_uploader.uploader import visibility_for
        self.assertEqual(
            visibility_for({"match_group": "Training_Room"}, self.s), "private")

    def test_it_reaches_create_replay(self):
        """End to end: the value actually lands in the RPC call."""
        self.client.meta = dict(self.client.meta, matchGroup="training_room")
        self.s["visibility"] = "public"
        self.make_replay("t.wowsreplay", mtime=time.time() - 100)
        for _ in range(4):
            self.eng.tick()
        self.assertEqual(self.client.created_visibility, "private")

    def test_a_real_battle_reaches_create_replay_unchanged(self):
        self.s["visibility"] = "public"
        self.battle("A", time.time() - 100)
        for _ in range(4):
            self.eng.tick()
        self.assertEqual(self.client.created_visibility, "public")
