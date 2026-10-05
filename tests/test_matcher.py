"""The matching engine, exercised against the cases from the spec (section 13).

Timing behaviour is the whole risk in this app, so it is tested against
synthetic event streams rather than a running game. Times are relative
seconds from an arbitrary session start; the engine only ever sees floats.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from debrief_uploader.config import DEFAULTS
from debrief_uploader.matcher import (
    AUTO, HOLD, NONE, AWAITING_SHOTS, UPLOADED, ReplayRec, ShotRec,
    cluster_shots, match_cluster, is_orphan, clock_skew, skew_is_sane,
)

T0 = 1_756_000_000.0    # session start, arbitrary epoch


def cfg(**over):
    c = dict(DEFAULTS)
    c.update(over)
    return c


def replay(key, close_min, group="pvp", state=AWAITING_SHOTS, **kw):
    """A replay that closed `close_min` minutes into the session."""
    return ReplayRec(key=key, t_close=T0 + close_min * 60, state=state,
                     match_group=group, **kw)


def shots(*offsets_min):
    return [ShotRec("shot%d.png" % i, T0 + m * 60) for i, m in enumerate(offsets_min)]


def one_cluster(offsets, c=None, now=None):
    c = c or cfg()
    sh = shots(*offsets)
    now = now if now is not None else max(s.t for s in sh) + c["pair_settle"] + 1
    closed, open_ = cluster_shots(sh, c["pair_gap"], c["pair_settle"], now,
                                  c["pair_settle_full"])
    assert len(closed) == 1, "expected one closed cluster, got %d" % len(closed)
    return closed[0]


class TestClustering(unittest.TestCase):

    def test_two_shots_seconds_apart_are_one_cluster(self):
        c = cfg()
        sh = [ShotRec("a", T0), ShotRec("b", T0 + 8)]
        closed, open_ = cluster_shots(sh, c["pair_gap"], c["pair_settle"], T0 + 200)
        self.assertEqual(len(closed), 1)
        self.assertEqual(len(closed[0]), 2)

    def test_two_battles_worth_of_shots_split(self):
        c = cfg()
        sh = [ShotRec("a", T0), ShotRec("b", T0 + 8),
              ShotRec("c", T0 + 900), ShotRec("d", T0 + 906)]
        closed, _ = cluster_shots(sh, c["pair_gap"], c["pair_settle"], T0 + 1200)
        self.assertEqual([len(g) for g in closed], [2, 2])

    def test_cluster_stays_open_until_settled(self):
        c = cfg()
        sh = [ShotRec("a", T0), ShotRec("b", T0 + 8)]
        closed, open_ = cluster_shots(sh, c["pair_gap"], c["pair_settle"], T0 + 20)
        self.assertEqual(closed, [])
        self.assertEqual(len(open_), 1)

    def test_chained_shots_within_gap_keep_one_cluster(self):
        # gap is measured from the PREVIOUS shot, not the first: a slow
        # three-tab capture is still one battle.
        c = cfg()
        sh = [ShotRec("a", T0), ShotRec("b", T0 + 100), ShotRec("c", T0 + 200)]
        closed, _ = cluster_shots(sh, c["pair_gap"], c["pair_settle"], T0 + 400)
        self.assertEqual(len(closed), 1)
        self.assertEqual(len(closed[0]), 3)


class TestSpecCases(unittest.TestCase):
    """The ten cases the matcher was designed against."""

    def test_01_happy_path(self):
        r = replay("A", 10)
        d = match_cluster(one_cluster([10 + 40 / 60.0, 10 + 50 / 60.0]), [r], cfg())
        self.assertEqual(d.kind, AUTO)
        self.assertEqual(d.replay.key, "A")

    def test_02_back_to_back_battles_four_minutes_apart(self):
        # the measured p05 gap. Both battles have their shots; each pair must
        # land on its own replay.
        a, b = replay("A", 10), replay("B", 14)
        ca = one_cluster([10.5, 10.6])
        da = match_cluster(ca, [a, b], cfg())
        self.assertEqual(da.kind, AUTO)
        self.assertEqual(da.replay.key, "A")
        a.state = UPLOADED                      # A is paired and gone
        db = match_cluster(one_cluster([14.5, 14.6]), [a, b], cfg())
        self.assertEqual(db.kind, AUTO)
        self.assertEqual(db.replay.key, "B")

    def test_03_skipped_battle_holds(self):
        # A never got screenshots; B's arrive. Nearest-preceding says B, and B
        # is probably right -- but A is still unpaired, so ask.
        a, b = replay("A", 10), replay("B", 25)
        d = match_cluster(one_cluster([25.5, 25.6]), [a, b], cfg())
        self.assertEqual(d.kind, HOLD)
        self.assertEqual(d.replay.key, "B")
        self.assertEqual([x.key for x in d.alternatives], ["A"])

    def test_04_early_exit_never_silently_attaches(self):
        # THE case. A closes 18s after starting (player left), B closes 12 min
        # later, A's scorecards arrive after B has already closed.
        a = replay("A", 0.3)
        b = replay("B", 12)
        d = match_cluster(one_cluster([13, 13.1]), [a, b], cfg())
        self.assertEqual(d.kind, HOLD, "early-exit shots must never auto-attach to B")
        self.assertEqual(d.replay.key, "B")
        self.assertIn("A", [x.key for x in d.alternatives])

    def test_05_three_shots_all_upload(self):
        r = replay("A", 10)
        c = one_cluster([10.5, 10.6, 10.7])
        d = match_cluster(c, [r], cfg())
        self.assertEqual(d.kind, AUTO)
        self.assertEqual(len(c), 3)

    def test_06_single_shot_holds(self):
        r = replay("A", 10)
        d = match_cluster(one_cluster([10.5]), [r], cfg())
        self.assertEqual(d.kind, HOLD)
        self.assertIn("one scorecard", d.reason)

    def test_07_training_room_takes_no_shots(self):
        # A training room has no scorecard screens, so shots after one belong
        # to neither it nor the battle before it.
        t = replay("T", 20, group="training_room")
        old = replay("A", 5)
        d = match_cluster(one_cluster([20.5, 20.6]), [old, t], cfg())
        self.assertEqual(d.kind, NONE)
        self.assertIn("training room", d.reason)

    def test_08_nine_hour_clock_skew_still_matches(self):
        # The filename/header clock is 9h ahead of the filesystem clock. The
        # engine only reads t_close, so the match is unaffected -- but the
        # skew must be reported as insane so the UI stops showing header time.
        r = replay("A", 10, header_dt=T0 + 10 * 60 + 9 * 3600)
        d = match_cluster(one_cluster([10.5, 10.6]), [r], cfg())
        self.assertEqual(d.kind, AUTO)
        self.assertFalse(skew_is_sane(clock_skew(r)))

    def test_08b_normal_clock_is_sane(self):
        r = replay("A", 10, header_dt=T0 + 10 * 60 - 600)   # 10 min battle
        self.assertTrue(skew_is_sane(clock_skew(r)))

    def test_09_shot_just_before_close_still_matches(self):
        # GRACE: a screenshot snapped 20s before the file finished closing.
        r = replay("A", 10)
        c = one_cluster([10 - 20 / 60.0, 10 + 5 / 60.0])
        d = match_cluster(c, [r], cfg())
        self.assertEqual(d.kind, AUTO)

    def test_09b_shot_well_before_close_does_not_match(self):
        r = replay("A", 10)
        d = match_cluster(one_cluster([5, 5.1]), [r], cfg())
        self.assertEqual(d.kind, NONE)

    def test_10_replay_with_no_shots_becomes_orphan(self):
        r = replay("A", 0)
        c = cfg()
        self.assertFalse(is_orphan(r, [r], T0 + 60, c))
        self.assertTrue(is_orphan(r, [r], T0 + c["orphan_after"] + 1, c))

    def test_10b_two_later_battles_orphan_it_early(self):
        a = replay("A", 0)
        rs = [a, replay("B", 16), replay("C", 32)]
        self.assertTrue(is_orphan(a, rs, T0 + 33 * 60, cfg()),
                        "player has clearly moved on")

    def test_10c_one_later_battle_does_not_orphan_it(self):
        a = replay("A", 0)
        rs = [a, replay("B", 8)]
        self.assertFalse(is_orphan(a, rs, T0 + 12 * 60, cfg()))


class TestGuards(unittest.TestCase):

    def test_too_old_is_refused(self):
        r = replay("A", 0)
        d = match_cluster(one_cluster([40, 40.1]), [r], cfg())
        self.assertEqual(d.kind, NONE)
        self.assertIn("earlier", d.reason)

    def test_already_uploaded_replay_is_not_a_candidate(self):
        r = replay("A", 10, state=UPLOADED)
        d = match_cluster(one_cluster([10.5, 10.6]), [r], cfg())
        self.assertEqual(d.kind, NONE)

    def test_too_many_shots_holds(self):
        r = replay("A", 10)
        c = one_cluster([10.5, 10.55, 10.6, 10.65, 10.7])
        d = match_cluster(c, [r], cfg())
        self.assertEqual(d.kind, HOLD)
        self.assertIn("5 screenshots", d.reason)

    def test_review_mode_holds_everything(self):
        r = replay("A", 10)
        d = match_cluster(one_cluster([10.5, 10.6]), [r], cfg(review_mode=True))
        self.assertEqual(d.kind, HOLD)

    def test_no_replays_at_all(self):
        d = match_cluster(one_cluster([10, 10.1]), [], cfg())
        self.assertEqual(d.kind, NONE)

    def test_older_unpaired_beyond_max_lag_does_not_block(self):
        # A is unpaired but far too old to be a plausible owner: don't make the
        # user resolve a choice that isn't real.
        a, b = replay("A", 0), replay("B", 40)
        d = match_cluster(one_cluster([40.5, 40.6]), [a, b], cfg())
        self.assertEqual(d.kind, AUTO)
        self.assertEqual(d.replay.key, "B")

    def test_older_unpaired_training_room_does_not_block(self):
        t, b = replay("T", 20, group="training_room"), replay("B", 30)
        d = match_cluster(one_cluster([30.5, 30.6]), [t, b], cfg())
        self.assertEqual(d.kind, AUTO)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSettleWindow(unittest.TestCase):
    """How long a group of screenshots waits before it is decided.

    A user watched two scorecards sit for 90 seconds and reasonably concluded
    it was broken. A group that already holds both tabs is complete, so it is
    decided quickly; a lone screenshot still waits, because its partner is
    exactly what is worth waiting for.
    """

    def test_pair_is_decided_quickly(self):
        c = cfg()
        sh = [ShotRec("a", T0), ShotRec("b", T0 + 6)]
        closed, open_ = cluster_shots(sh, c["pair_gap"], c["pair_settle"],
                                      T0 + 6 + c["pair_settle_full"] + 1,
                                      c["pair_settle_full"])
        self.assertEqual(len(closed), 1)
        self.assertEqual(open_, [])

    def test_pair_is_not_decided_before_its_short_window(self):
        c = cfg()
        sh = [ShotRec("a", T0), ShotRec("b", T0 + 6)]
        closed, open_ = cluster_shots(sh, c["pair_gap"], c["pair_settle"],
                                      T0 + 10, c["pair_settle_full"])
        self.assertEqual(closed, [])
        self.assertEqual(len(open_), 1)

    def test_single_screenshot_waits_the_long_window(self):
        c = cfg()
        sh = [ShotRec("a", T0)]
        closed, open_ = cluster_shots(sh, c["pair_gap"], c["pair_settle"],
                                      T0 + c["pair_settle_full"] + 1,
                                      c["pair_settle_full"])
        self.assertEqual(closed, [], "keep waiting for the second tab")
        closed, _ = cluster_shots(sh, c["pair_gap"], c["pair_settle"],
                                  T0 + c["pair_settle"] + 1,
                                  c["pair_settle_full"])
        self.assertEqual(len(closed), 1)

    def test_decides_at_is_reportable(self):
        from debrief_uploader.matcher import decides_at
        c = cfg()
        pair = one_cluster([0, 0.1], c=c)
        self.assertEqual(decides_at(pair, c["pair_settle"], c["pair_settle_full"]),
                         pair.t_last + c["pair_settle_full"])

    def test_a_third_shot_still_joins_before_the_window_closes(self):
        c = cfg()
        sh = [ShotRec("a", T0), ShotRec("b", T0 + 6), ShotRec("c", T0 + 20)]
        closed, _ = cluster_shots(sh, c["pair_gap"], c["pair_settle"],
                                  T0 + 20 + c["pair_settle_full"] + 1,
                                  c["pair_settle_full"])
        self.assertEqual(len(closed), 1)
        self.assertEqual(len(closed[0]), 3)


class TestOlderBattleMustBePlausible(unittest.TestCase):
    """A battle you never screenshotted must not block every battle after it.

    Reported from a real machine: an 08:56 game that had ended by 09:11 at the
    latest was still being treated as a rival claimant for 09:26 screenshots
    that plainly belonged to the 09:12 game. One skipped battle poisoned the
    whole session, and the held count only grew.

    The guard is kept where it earns its place: an older battle whose results
    could genuinely still be on screen (an early exit whose battle ran on) is
    still a real ambiguity and is still held.
    """

    def battle(self, key, start_min, close_min, duration=900):
        """start/close in minutes from T0; header_dt is the game clock."""
        return ReplayRec(key=key, t_close=T0 + close_min * 60,
                         state=AWAITING_SHOTS, match_group="pvp",
                         header_dt=T0 + start_min * 60, duration=duration)

    def test_the_reported_case_now_uploads(self):
        # A: starts 0, 15-min limit -> cannot run past minute 15
        # B: starts 16, closes 29;  screenshots at 30
        a = self.battle("A", 0, 12)
        b = self.battle("B", 16, 29)
        d = match_cluster(one_cluster([30, 30.1]), [a, b], cfg())
        self.assertEqual(d.kind, AUTO, "A ended 15 min before these were taken")
        self.assertEqual(d.replay.key, "B")

    def test_early_exit_still_holds(self):
        # A starts at 16 and the player leaves at 17 -- but A runs until 31,
        # so its results appear AFTER B closed. That is the real ambiguity.
        a = self.battle("A", 16, 17)
        b = self.battle("B", 18, 29)
        d = match_cluster(one_cluster([31, 31.1]), [a, b], cfg())
        self.assertEqual(d.kind, HOLD)
        self.assertIn("A", [x.key for x in d.alternatives])

    def test_unknown_duration_stays_conservative(self):
        a = ReplayRec(key="A", t_close=T0 + 12 * 60, state=AWAITING_SHOTS,
                      match_group="pvp", header_dt=T0, duration=None)
        b = self.battle("B", 16, 29)
        d = match_cluster(one_cluster([30, 30.1]), [a, b], cfg())
        self.assertEqual(d.kind, HOLD, "never guess from a missing timestamp")

    def test_skewed_clock_stays_conservative(self):
        # The -9h archive case: header_dt is on a different clock, so the
        # elapsed-time subtraction is meaningless and must not be trusted.
        a = ReplayRec(key="A", t_close=T0 + 12 * 60, state=AWAITING_SHOTS,
                      match_group="pvp", header_dt=T0 + 12 * 60 + 9 * 3600,
                      duration=900)
        b = self.battle("B", 16, 29)
        d = match_cluster(one_cluster([30, 30.1]), [a, b], cfg())
        self.assertEqual(d.kind, HOLD)

    def test_results_window_end_maths(self):
        from debrief_uploader.matcher import results_window_end
        # left 60s into a 900s battle -> 840s of battle still to run
        r = ReplayRec(key="A", t_close=T0 + 60, header_dt=T0, duration=900)
        self.assertEqual(results_window_end(r, 0), T0 + 60 + 840)
        # played it out -> results are up at the close
        r2 = ReplayRec(key="B", t_close=T0 + 900, header_dt=T0, duration=900)
        self.assertEqual(results_window_end(r2, 0), T0 + 900)

    def test_a_skipped_battle_does_not_snowball(self):
        """Three battles, only the last screenshotted: the two skipped ones
        must not accumulate into a permanent block."""
        rs = [self.battle("A", 0, 13), self.battle("B", 16, 29),
              self.battle("C", 32, 45)]
        d = match_cluster(one_cluster([46, 46.1]), rs, cfg())
        self.assertEqual(d.kind, AUTO)
        self.assertEqual(d.replay.key, "C")
