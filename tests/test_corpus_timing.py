"""Replay the real archive's battle cadence through the matching engine.

Synthetic unit tests prove the rules; this proves the rules survive the actual
distribution of the user's play -- including the 5% of battles that close
within about five minutes of each other, which is what makes a naive time
window wrong. Skipped when the archive isn't on this machine.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from debrief_uploader.config import DEFAULTS
from debrief_uploader.matcher import (
    AUTO, HOLD, AWAITING_SHOTS, UPLOADED, ReplayRec, ShotRec,
    cluster_shots, match_cluster,
)

ARCHIVE = os.path.expanduser("~/WowsLegendsReplayArchive")
NAMED = re.compile(r"^(\d{8})_(\d{6})_")


def load_closes():
    """(mtime, name) for every archived replay, oldest first."""
    out = []
    try:
        names = os.listdir(ARCHIVE)
    except OSError:
        return out
    for n in names:
        if n.endswith(".wowsreplay") and NAMED.match(n):
            out.append((os.path.getmtime(os.path.join(ARCHIVE, n)), n))
    out.sort()
    return out


def sessions(closes, split=2 * 3600):
    """Split the archive into play sessions on gaps larger than `split`."""
    cur, out = [], []
    for c in closes:
        if cur and c[0] - cur[-1][0] > split:
            out.append(cur)
            cur = []
        cur.append(c)
    if cur:
        out.append(cur)
    return out


class TestAgainstRealCadence(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.closes = load_closes()
        if len(cls.closes) < 50:
            raise unittest.SkipTest("replay archive not on this machine")

    def _run_session(self, batch, shot_delay, cfg):
        """Simulate: after each battle closes, the player takes two scorecards
        `shot_delay` seconds later. Every cluster must land on its own battle."""
        replays = [ReplayRec(key=n, t_close=t, state=AWAITING_SHOTS,
                             match_group="pvp") for t, n in batch]
        by_key = {r.key: r for r in replays}
        correct = wrong = held = missed = 0
        for t, n in batch:
            sh = [ShotRec("a", t + shot_delay), ShotRec("b", t + shot_delay + 7)]
            now = sh[-1].t + cfg["pair_settle"] + 1
            closed, _ = cluster_shots(sh, cfg["pair_gap"], cfg["pair_settle"], now)
            d = match_cluster(closed[0], replays, cfg)
            if d.kind == AUTO and d.replay.key == n:
                correct += 1
                by_key[n].state = UPLOADED
            elif d.kind == AUTO:
                wrong += 1
                d.replay.state = UPLOADED
            elif d.kind == HOLD:
                held += 1
            else:
                missed += 1
        return correct, wrong, held, missed

    def test_every_battle_pairs_with_itself(self):
        cfg = dict(DEFAULTS)
        tot = [0, 0, 0, 0]
        for batch in sessions(self.closes):
            for i, v in enumerate(self._run_session(batch, 45, cfg)):
                tot[i] += v
        correct, wrong, held, missed = tot
        n = sum(tot)
        print("\n  %d battles: %d correct, %d WRONG, %d held, %d unmatched"
              % (n, correct, wrong, held, missed))
        self.assertEqual(wrong, 0, "a wrong auto-match silently poisons the corpus")
        self.assertEqual(held, 0, "a diligent player should never be asked")
        self.assertEqual(missed, 0)
        self.assertEqual(correct, n)

    def test_slow_screenshotter_still_correct(self):
        """Even a player who takes four minutes to grab the scorecards never
        gets a wrong auto-match -- at worst the engine asks."""
        cfg = dict(DEFAULTS)
        wrong_total = 0
        for delay in (10, 60, 120, 240):
            for batch in sessions(self.closes):
                _, wrong, _, _ = self._run_session(batch, delay, cfg)
                wrong_total += wrong
        self.assertEqual(wrong_total, 0)

    def test_skipping_screenshots_is_caught_not_guessed(self):
        """The player screenshots only every other battle. Every decision must
        be either right or a hold -- never a wrong upload."""
        cfg = dict(DEFAULTS)
        wrong = held = correct = 0
        for batch in sessions(self.closes):
            replays = [ReplayRec(key=n, t_close=t, state=AWAITING_SHOTS,
                                 match_group="pvp") for t, n in batch]
            for i, (t, n) in enumerate(batch):
                if i % 2:
                    continue                      # skipped this battle's shots
                sh = [ShotRec("a", t + 45), ShotRec("b", t + 52)]
                closed, _ = cluster_shots(sh, cfg["pair_gap"], cfg["pair_settle"],
                                          sh[-1].t + cfg["pair_settle"] + 1)
                d = match_cluster(closed[0], replays, cfg)
                if d.kind == AUTO and d.replay.key == n:
                    correct += 1
                    d.replay.state = UPLOADED
                elif d.kind == AUTO:
                    wrong += 1
                    d.replay.state = UPLOADED
                elif d.kind == HOLD:
                    held += 1
        print("\n  every-other-battle: %d correct, %d WRONG, %d held"
              % (correct, wrong, held))
        self.assertEqual(wrong, 0, "must never guess when an earlier battle is unpaired")

    def test_tightest_real_gaps_are_handled(self):
        """Sanity: the corpus really does contain the fast back-to-back gaps
        that make this hard -- otherwise the tests above prove nothing."""
        gaps = sorted(b[0] - a[0] for a, b in zip(self.closes, self.closes[1:])
                      if 0 < b[0] - a[0] < 7200)
        self.assertGreater(len(gaps), 300)
        p05 = gaps[int(0.05 * len(gaps))]
        tight = sum(1 for g in gaps if g < 300)
        print("\n  %d in-session gaps, p05 = %.0fs, %d under 5 min" % (len(gaps), p05, tight))
        # The COUNT of genuinely tight gaps is the substantive claim: without
        # them the matching tests above would be proving nothing. The
        # percentile is a loose sanity bound only -- this archive grows every
        # time Greg plays, and an earlier version pinned p05 < 330 s and broke
        # a day later when it drifted to 331.
        self.assertGreater(tight, 10, "corpus must contain real sub-5-minute gaps")
        self.assertLess(p05, 600, "back-to-back battles should still be common")


if __name__ == "__main__":
    unittest.main(verbosity=2)
