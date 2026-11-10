"""The battle time printed on a result screen decides which battle it is.

Every result screen prints "DD.MM.YYYY HH:MM" under the mode/map line. That
minute sits 3-148 s before the replay header's dateTime for the same battle
(measured on 1,837 screenshots); minutes apart means another battle; hours or
days apart means the PC clock is off and says nothing.

The case this exists for: two battles' results reopened from port long after
playing, all four tabs captured within 40 seconds. By file time that is one
group, so it was matched to the wrong battle or to none at all.
"""
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image, ImageDraw, ImageFont

from debrief_uploader import matcher, screenocr, screentime as st, watcher
from debrief_uploader.api import iso_utc
from debrief_uploader.config import DEFAULTS
from debrief_uploader.matcher import (AWAITING_SHOTS, ORPHAN, ReplayRec,
                                      ShotRec, UPLOADED)

sys.path.insert(0, os.path.dirname(__file__))
from test_engine import EngineCase, FakeClient  # noqa: E402

P = st.parse_screen_time
H = st.parse_header_time


class TestReadingTheLine(unittest.TestCase):

    def test_plain(self):
        self.assertEqual(P("09.11.2026 21:43"), datetime(2026, 11, 9, 21, 43))

    def test_ocr_habits(self):
        self.assertEqual(P("27.10.2026 18:30ł"), datetime(2026, 10, 27, 18, 30))
        self.assertEqual(P("28.10,2026 10:06"), datetime(2026, 10, 28, 10, 6))
        self.assertEqual(P("09.11.2026 21 :43"), datetime(2026, 11, 9, 21, 43))

    def test_year_first(self):
        self.assertEqual(P("2026/11/09 21:43"), datetime(2026, 11, 9, 21, 43))

    def test_refusals(self):
        self.assertIsNone(P("09/11/2026 21:43"), "D/M vs M/D cannot be told apart")
        self.assertIsNone(P("STANDARD - DOMINATION - TRAP"))
        self.assertIsNone(P("31.02.2026 10:00"))
        self.assertIsNone(P("17.9 K @ 9.7 M ® 1.8 MA"), "currency strip")
        self.assertIsNone(P(None))

    def test_header_lines_top_down(self):
        self.assertEqual(st.find_screen_header(
            ["PORT", "SHIPS", "VICTORY!", "17.9 K @ 9.7 M ® 1.8 MA",
             "STANDARD — DOMINATION — WARRIOR'S PATH", "09.11.2026 20:27"]),
            (datetime(2026, 11, 9, 20, 27),
             "STANDARD — DOMINATION — WARRIOR'S PATH"))
        self.assertEqual(st.find_screen_header(["VICTORY!", "nothing"]), (None, None))

    def test_header_time_both_forms(self):
        self.assertEqual(H("09.11.2026 21:43:16"), datetime(2026, 11, 9, 21, 43, 16))
        self.assertEqual(H(iso_utc("09.11.2026 21:43:16")),
                         datetime(2026, 11, 9, 21, 43, 16),
                         "played_at keeps the wall clock, no zone conversion")
        self.assertIsNone(H(None))
        self.assertIsNone(H("garbage"))

    def test_storage_round_trip(self):
        dt = datetime(2026, 11, 9, 20, 27)
        self.assertEqual(st.from_text(st.to_text(dt)), dt)
        self.assertEqual(st.to_text(None), "")
        self.assertIsNone(st.from_text(""))
        self.assertIsNone(st.from_text(None))

    def test_map_line(self):
        self.assertTrue(st.names_map("STANDARD - DOMINATION - WARRIOR'S PATH",
                                     "Warrior's Path"))
        self.assertFalse(st.names_map("RANKED - DOMINATION - POLAR", "Greece"))
        self.assertFalse(st.names_map(None, "Greece"))


class TestRelation(unittest.TestCase):
    """Real measured pairs: (screen line, header dateTime, verdict)."""

    PAIRS = [
        ("09.11.2026 21:43", "09.11.2026 21:43:16", st.MATCH),
        ("09.11.2026 17:45", "09.11.2026 17:46:05", st.MATCH),
        ("29.10.2026 12:01", "29.10.2026 12:03:24", st.MATCH),       # widest seen
        ("27.10.2026 18:30ł", "27.10.2026 18:30:20", st.MATCH),
        # a PC clock set four days ahead: inconclusive, never "another battle"
        ("27.10.2026 15:35", "31.10.2026 15:35:49", st.CLOCK_OFFSET),
        ("27.10.2026 15:08", "31.10.2026 15:08:46", st.CLOCK_OFFSET),
        ("27.10.2026 15:01", "31.10.2026 15:07:07", st.CLOCK_OFFSET),
        # another battle's screen, later or earlier than this recording
        ("13.10.2026 10:56", "13.10.2026 10:51:10", st.CONTRADICTS),
        ("13.10.2026 10:56", "13.10.2026 11:00:54", st.CONTRADICTS),
        ("15.10.2026 15:46", "15.10.2026 15:36:28", st.CONTRADICTS),
        ("19.07.2026 16:49", "19.07.2026 16:55:18", st.CONTRADICTS),
        ("24.10.2026 12:48", "24.10.2026 13:16:20", st.CONTRADICTS),
    ]

    def test_measured_pairs(self):
        for scr, hdr, want in self.PAIRS:
            self.assertEqual(st.relation(P(scr), H(hdr)), want, (scr, hdr))

    def test_late_join(self):
        self.assertEqual(st.relation(datetime(2026, 7, 28, 13, 28),
                                     datetime(2026, 7, 28, 13, 38, 26),
                                     joined_late=True), st.LATE_JOIN)
        self.assertEqual(st.relation(datetime(2026, 10, 15, 15, 46),
                                     datetime(2026, 10, 15, 15, 36, 28),
                                     joined_late=True), st.CONTRADICTS,
                         "a late join never excuses a LATER screen")

    def test_unreadable_and_zone_shift(self):
        self.assertIsNone(st.relation(None, datetime(2026, 1, 1)))
        self.assertEqual(st.relation(datetime(2026, 11, 9, 22, 43),
                                     datetime(2026, 11, 9, 21, 43, 16)),
                         st.CLOCK_OFFSET)

    def test_machine_offset(self):
        ahead = [(P(s), H(h)) for s, h, w in self.PAIRS if w == st.CLOCK_OFFSET]
        off = st.machine_offset(ahead, spread_s=400)
        self.assertEqual(round(off / 86400), -4)
        self.assertEqual(st.relation(ahead[0][0], ahead[0][1], off), st.MATCH)
        right = [(P(s), H(h)) for s, h, w in self.PAIRS if w == st.MATCH]
        self.assertEqual(st.machine_offset(right), 0.0)
        self.assertEqual(st.machine_offset(right[:2] + ahead[:2]), 0.0,
                         "pairs that disagree move nothing")
        self.assertEqual(st.machine_offset(ahead[:2]), 0.0, "too few pairs")


# One evening's battles by header start, and four tabs of the 20:27 and 20:49
# battles captured from port at 22:03-22:04.
SESSION = [("b1", "09.11.2026 17:11:40"), ("b2", "09.11.2026 17:24:12"),
           ("b3", "09.11.2026 17:46:05"), ("A", "09.11.2026 20:27:54"),
           ("B", "09.11.2026 20:49:37"), ("b6", "09.11.2026 21:02:18"),
           ("b7", "09.11.2026 21:21:22"), ("b8", "09.11.2026 21:43:16")]
BURST = [("220336", "09.11.2026 20:27"), ("220353", "09.11.2026 20:27"),
         ("220406", "09.11.2026 20:49"), ("220411", "09.11.2026 20:49")]


class TestPickAndSplit(unittest.TestCase):

    def setUp(self):
        self.battles = [(k, H(h), None) for k, h in SESSION]

    def test_one_burst_two_battles(self):
        groups = st.split_by_screen_time([(n, P(t)) for n, t in BURST])
        self.assertEqual(groups, [["220336", "220353"], ["220406", "220411"]])
        self.assertEqual(st.pick_battle(P("09.11.2026 20:27"), self.battles), "A")
        self.assertEqual(st.pick_battle(P("09.11.2026 20:49"), self.battles), "B")

    def test_split_keeps_unread_with_neighbour(self):
        self.assertEqual(st.split_by_screen_time(
            [("a", None), ("b", datetime(2026, 1, 1, 1, 1)), ("c", None)]),
            [["a", "b", "c"]])

    def test_split_tolerates_a_neighbouring_minute(self):
        self.assertEqual(st.split_by_screen_time(
            [("a", datetime(2026, 1, 1, 1, 1)), ("b", datetime(2026, 1, 1, 1, 2))]),
            [["a", "b"]])

    def test_nothing_or_two_fit(self):
        self.assertIsNone(st.pick_battle(P("09.11.2026 19:00"), self.battles))
        self.assertIsNone(st.pick_battle(
            datetime(2026, 11, 9, 21, 43),
            self.battles + [("dup", datetime(2026, 11, 9, 21, 43, 40), None)]),
            "never guess between two")
        self.assertIsNone(st.pick_battle(None, self.battles))

    def test_late_join_only_on_its_map(self):
        b = [("j", datetime(2026, 7, 14, 19, 3, 32), "Warrior's Path")]
        self.assertEqual(st.pick_battle(datetime(2026, 7, 14, 18, 58), b,
                                        mode_line="STANDARD - DOMINATION - WARRIOR'S PATH"),
                         "j")
        self.assertIsNone(st.pick_battle(datetime(2026, 7, 14, 18, 58), b,
                                         mode_line="STANDARD - DOMINATION - NORTH"))


def _epoch(s):
    return time.mktime(time.strptime(s, "%d.%m.%Y %H:%M:%S"))


class TestMatcherWithScreenTime(unittest.TestCase):
    """match_by_screen on the evening above, as the app would see it at
    22:04: every battle uploaded without scorecards except the last, which
    is still waiting for its own."""

    def setUp(self):
        self.recs = []
        for k, h in SESSION:
            start = _epoch(h)
            self.recs.append(ReplayRec(
                key=k, t_close=start + 15 * 60, state=UPLOADED, match_group="pvp",
                header_dt=start, duration=1200, played_at=iso_utc(h),
                remote_id="rid-" + k))
        self.recs[-1].state = AWAITING_SHOTS
        self.recs[-1].remote_id = None
        self.shots = [ShotRec(n, _epoch("09.11.2026 %s:%s:%s"
                                        % (n[:2], n[2:4], n[4:])))
                      for n, _t in BURST]
        self.cfg = dict(DEFAULTS)

    def test_positive_control_file_time_alone_gets_it_wrong(self):
        closed, open_ = matcher.cluster_shots(
            self.shots, self.cfg["pair_gap"], 0, self.shots[-1].t + 600)
        self.assertEqual(len(closed), 1, "the 120 s gap joins all four tabs")
        d = matcher.match_cluster(closed[0], self.recs, self.cfg)
        self.assertEqual(d.kind, matcher.AUTO)
        self.assertEqual(d.replay.key, "b8",
                         "by file time all four go to the battle still waiting "
                         "- neither of the two they are of")

    def test_screen_time_splits_and_names_both_battles(self):
        closed, _ = matcher.cluster_shots(
            self.shots, self.cfg["pair_gap"], 0, self.shots[-1].t + 600)
        parts = st.split_by_screen_time(
            [(s, P(dict(BURST)[s.path])) for s in closed[0].shots])
        got = []
        for part in parts:
            screen = P(dict(BURST)[part[0].path])
            d = matcher.match_by_screen(matcher.Cluster(part), screen, None,
                                        self.recs, self.cfg)
            got.append((d.kind, d.replay.key, [s.path for s in part]))
        self.assertEqual(got, [
            (matcher.LATE, "A", ["220336", "220353"]),
            (matcher.LATE, "B", ["220406", "220411"])])

    def test_unread_time_is_exactly_the_old_rule(self):
        c = matcher.Cluster(self.shots[:2])
        old = matcher.match_cluster(c, self.recs, self.cfg)
        new = matcher.match_by_screen(c, None, None, self.recs, self.cfg)
        self.assertEqual((new.kind, new.replay.key, new.reason),
                         (old.kind, old.replay.key, old.reason))

    def test_a_contradicting_time_attaches_nothing(self):
        # a battle nobody here knows of (played before watching started)
        c = matcher.Cluster(self.shots[:2])
        d = matcher.match_by_screen(c, datetime(2026, 11, 9, 16, 0), None,
                                    self.recs, self.cfg)
        self.assertEqual(d.kind, matcher.NONE)
        self.assertIsNone(d.replay)
        self.assertTrue(d.by_screen)

    def test_a_clock_days_off_falls_back_to_file_time(self):
        c = matcher.Cluster(self.shots[:2])
        d = matcher.match_by_screen(c, datetime(2026, 11, 5, 22, 3), None,
                                    self.recs, self.cfg)
        self.assertEqual((d.kind, d.replay.key), (matcher.AUTO, "b8"))

    def test_a_battle_that_has_its_screenshots_keeps_them(self):
        self.recs[3].n_shots = 2
        d = matcher.match_by_screen(matcher.Cluster(self.shots[:2]),
                                    P("09.11.2026 20:27"), None, self.recs, self.cfg)
        self.assertEqual(d.kind, matcher.NONE, "never handed to another battle")

    def test_a_battle_the_user_chose_no_screenshots_for(self):
        self.recs[3].hand_picked = True
        d = matcher.match_by_screen(matcher.Cluster(self.shots[:2]),
                                    P("09.11.2026 20:27"), None, self.recs, self.cfg)
        self.assertEqual(d.kind, matcher.NONE)

    def test_waiting_or_given_up_battles_are_paired(self):
        self.recs[3].state, self.recs[3].remote_id = ORPHAN, None
        d = matcher.match_by_screen(matcher.Cluster(self.shots[:2]),
                                    P("09.11.2026 20:27"), None, self.recs, self.cfg)
        self.assertEqual((d.kind, d.replay.key), (matcher.AUTO, "A"))
        self.recs[3].state = AWAITING_SHOTS
        d = matcher.match_by_screen(matcher.Cluster(self.shots[:2]),
                                    P("09.11.2026 20:27"), None, self.recs, self.cfg)
        self.assertEqual((d.kind, d.replay.key), (matcher.AUTO, "A"),
                         "the time settles the older-battle ambiguity")

    def test_one_tab_or_review_mode_holds(self):
        d = matcher.match_by_screen(matcher.Cluster(self.shots[:1]),
                                    P("09.11.2026 20:27"), None, self.recs, self.cfg)
        self.assertEqual((d.kind, d.replay.key), (matcher.HOLD, "A"))
        self.cfg["review_mode"] = True
        d = matcher.match_by_screen(matcher.Cluster(self.shots[:2]),
                                    P("09.11.2026 20:27"), None, self.recs, self.cfg)
        self.assertEqual((d.kind, d.replay.key), (matcher.HOLD, "A"))


class MetaClient(FakeClient):
    """FakeClient whose replay-meta answers per replay file."""

    def __init__(self):
        super().__init__()
        self.by_raw = {}

    def replay_meta(self, raw):
        self.calls.append("meta")
        self.meta_reads = getattr(self, "meta_reads", 0) + 1
        return dict(self.by_raw.get(raw, self.meta))


def _wall(epoch):
    return time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(epoch))


def _minute(epoch):
    return datetime.fromtimestamp(epoch).replace(second=0, microsecond=0)


class ScreenEngineCase(EngineCase):

    def setUp(self):
        super().setUp()
        self.client = MetaClient()
        self.eng.client = self.client
        self.screen = {}            # shot file name -> datetime | None
        self.ocr_calls = []

        def reader(paths):
            self.ocr_calls.append(list(paths))
            return {p: (self.screen.get(os.path.basename(p)), None)
                    for p in paths if os.path.basename(p) in self.screen}
        self.eng.read_screen_headers = reader

    def replay(self, tag, start, close):
        """A replay whose header says it began at `start`."""
        p = self.make_replay("2026_%s.wowsreplay" % tag, mtime=close)
        with open(p, "rb") as f:
            raw = f.read()
        self.client.by_raw[raw] = dict(self.client.meta, dateTime=_wall(start),
                                       mapName="spaces/%s" % tag)
        return p

    def row(self, tag):
        return next(r for r in self.store.replays()
                    if r["filename"] == "2026_%s.wowsreplay" % tag)

    def ticks(self, n=8):
        for _ in range(n):
            self.eng.tick()


class LateSetsBase(ScreenEngineCase):
    """Greg's case: two battles 22 minutes apart, their results reopened
    from port 90+ minutes later and all four tabs captured within 40 s,
    while a newer battle sits waiting for its own scorecards."""

    def make_tab(self, name, mtime, when):
        self.make_shot(name, mtime=mtime)

    def setUp(self):
        super().setUp()
        now = time.time()
        self.now = now
        self.hA = now - 140 * 60
        self.hB = self.hA + 22 * 60
        self.replay("A", self.hA, self.hA + 14 * 60)
        self.replay("B", self.hB, self.hB + 15 * 60)
        self.replay("D", now - 14 * 60, now - 3 * 60)     # still waiting
        self.ticks()
        # A and B gave up on scorecards and went up without them
        self.assertEqual(self.row("A")["state"], UPLOADED)
        self.assertEqual(self.row("B")["state"], UPLOADED)
        self.assertEqual(self.row("D")["state"], AWAITING_SHOTS)
        self.burst = []
        for name, dt, start in (("p1.png", -75, self.hA), ("p2.png", -58, self.hA),
                                ("p3.png", -45, self.hB), ("p4.png", -40, self.hB)):
            self.make_tab(name, now + dt, _minute(start))
            self.screen[name] = _minute(start)
            self.burst.append(name)
        self.assertGreater(now - 75 - (self.hB + 15 * 60), 90 * 60,
                           "the tabs are taken 90+ minutes after the battles")

    def check_each_set_goes_to_its_own_uploaded_battle(self):
        self.ticks()
        a, b, d = self.row("A"), self.row("B"), self.row("D")
        self.assertEqual([os.path.basename(s["path"])
                          for s in self.store.shots_for(a["md5"])], ["p1.png", "p2.png"])
        self.assertEqual([os.path.basename(s["path"])
                          for s in self.store.shots_for(b["md5"])], ["p3.png", "p4.png"])
        self.assertEqual(self.store.shots_for(d["md5"]), [],
                         "the waiting battle does not get another battle's tabs")
        self.assertEqual(d["state"], AWAITING_SHOTS)
        for r in (a, b):
            self.assertEqual(r["state"], UPLOADED)
            self.assertEqual(r["late_shots"], 0)
            self.assertEqual(sorted(self.client.rows[r["remote_id"]]["shots"]),
                             ["%s/shot1.jpg" % r["remote_id"],
                              "%s/shot2.jpg" % r["remote_id"]])
        self.assertEqual(self.client.calls.count("rpc:reprocess_own_replay"), 2)
        self.assertEqual(self.client.calls.count("rpc:create_replay"), 2,
                         "no second row for a battle already up")


class TestLateScreenshotsFromPort(LateSetsBase):

    def test_positive_control_without_the_time(self):
        # No time read (Linux, no OCR, unreadable): today's behaviour, which
        # gives all four tabs of two old battles to the one still waiting.
        self.screen.clear()
        self.ticks()
        self.assertEqual(len(self.store.shots_for(self.row("D")["md5"])), 4)
        self.assertEqual(self.store.shots_for(self.row("A")["md5"]), [])

    def test_each_set_goes_to_its_own_uploaded_battle(self):
        self.check_each_set_goes_to_its_own_uploaded_battle()

    def test_each_screenshot_is_read_once(self):
        self.ticks()
        self.ticks()
        read = [os.path.basename(p) for call in self.ocr_calls for p in call]
        self.assertEqual(sorted(read), sorted(self.burst))

    def test_review_mode_asks_first_and_skip_leaves_the_battle_alone(self):
        self.s["review_mode"] = True
        self.ticks()
        a = self.row("A")
        self.assertEqual(a["state"], matcher.HELD)
        self.assertEqual(self.client.calls.count("rpc:reprocess_own_replay"), 0)
        self.eng.skip(a["md5"])
        a = self.row("A")
        self.assertEqual(a["state"], UPLOADED, "still on the site, as it was")
        self.assertEqual(self.store.shots_for(a["md5"]), [])

    def test_review_mode_confirm_adds_them(self):
        self.s["review_mode"] = True
        self.ticks()
        self.eng.confirm(self.row("A")["md5"])
        self.ticks(3)
        a = self.row("A")
        self.assertEqual(a["state"], UPLOADED)
        self.assertEqual(len(self.client.rows[a["remote_id"]]["shots"]), 2)
        self.assertEqual(self.client.calls.count("rpc:create_replay"), 2)

    def test_a_row_that_refuses_them_is_given_up_on(self):
        a = self.row("A")
        del self.client.rows[a["remote_id"]]       # deleted on the site
        for _ in range(12):
            self.ticks(1)
            r = self.row("A")
            if r["state"] == UPLOADED and not r["late_shots"] and r["attempts"]:
                break
            self.store.update_replay(r["md5"], next_try=0)
        r = self.row("A")
        self.assertEqual((r["state"], r["late_shots"]), (UPLOADED, 0))
        self.assertIsNone(self.eng.blocked)


class TestScreenTimeGuards(ScreenEngineCase):

    def test_unreadable_time_behaves_exactly_as_before(self):
        now = time.time()
        self.battle("X", now - 900)
        self.battle("Y", now - 600)
        self.screen.update({n: None for n in ("shot_X_a.png", "shot_X_b.png",
                                              "shot_Y_a.png", "shot_Y_b.png")})
        self.ticks()
        for row in self.store.replays():
            self.assertEqual(row["state"], UPLOADED)
            self.assertEqual(len(self.store.shots_for(row["md5"])), 2)
        self.assertTrue(self.ocr_calls, "the reader was asked")
        self.assertTrue(all(s["screen_time"] == "" for s in self.store.all_shots()))

    def test_normal_battle_matches_with_its_time_read(self):
        now = time.time()
        start = now - 1300
        self.replay("N", start, now - 600)
        self.make_shot("n1.png", mtime=now - 560)
        self.make_shot("n2.png", mtime=now - 553)
        self.screen.update({"n1.png": _minute(start), "n2.png": _minute(start)})
        self.ticks()
        self.assertEqual(self.row("N")["state"], UPLOADED)
        self.assertEqual(len(self.store.shots_for(self.row("N")["md5"])), 2)

    def test_battle_from_before_watching_started_stays_unread(self):
        # Privacy: a replay older than watching_since is never read. Its
        # results reopened from port now must not be attached to the battle
        # that IS watched, and must not cause the old replay to be read.
        now = time.time()
        self.s["watching_since"] = now - 20 * 60
        old_start = now - 3 * 3600
        self.replay("OLD", old_start, old_start + 900)
        self.replay("W", now - 15 * 60, now - 4 * 60)
        self.make_shot("o1.png", mtime=now - 70)
        self.make_shot("o2.png", mtime=now - 60)
        self.screen.update({"o1.png": _minute(old_start), "o2.png": _minute(old_start)})
        self.ticks()
        names = [r["filename"] for r in self.store.replays()]
        self.assertEqual(names, ["2026_W.wowsreplay"], "the old replay is never read")
        self.assertEqual(self.client.calls.count("meta"), 1)
        w = self.row("W")
        self.assertEqual(self.store.shots_for(w["md5"]), [])
        self.assertEqual(w["state"], AWAITING_SHOTS)
        self.assertEqual(len(self.client.rows), 0, "nothing uploaded")

    def test_positive_control_old_shots_without_time_would_attach(self):
        now = time.time()
        self.s["watching_since"] = now - 20 * 60
        self.replay("W", now - 15 * 60, now - 4 * 60)
        self.make_shot("o1.png", mtime=now - 70)
        self.make_shot("o2.png", mtime=now - 60)
        self.ticks()
        self.assertEqual(len(self.store.shots_for(self.row("W")["md5"])), 2)

    def test_learnt_clock_offset_from_matched_pairs(self):
        # three earlier matches on a PC whose clock runs 4 days ahead of the
        # screen: the offset is learnt and a later burst then splits right
        for i in range(3):
            self.store.add_shot("/x/%d.png" % i, 0)
            self.store.set_screen_header("/x/%d.png" % i, "2026-10-27 15:%02d" % (10 * i),
                                         None)
            self.store.add_replay(md5="m%d" % i, played_at="2026-10-31T15:%02d:40Z" % (10 * i),
                                  state=UPLOADED, t_close=0)
            self.store.attach_shots(["/x/%d.png" % i], "m%d" % i)
        off = self.eng._clock_offset()
        self.assertEqual(round(off / 86400), -4)


# ---- reading the time off an image ------------------------------------------

def _font(size):
    for name in ("bahnschrift.ttf", "arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def result_screen(path, w=1920, h=1080, when="09.11.2026 20:27",
                  mode="STANDARD — DOMINATION — TRAP", bars=0):
    """A stand-in for a result screen: the header corner the game draws
    (light text on a dark sea), the rest empty. `bars` adds ultrawide black
    side bars of that width."""
    im = Image.new("RGB", (w + 2 * bars, h), (0, 0, 0))
    pic = Image.new("RGB", (w, h), (34, 44, 54))
    d = ImageDraw.Draw(pic)
    k = h / 1080.0
    x = int(80 * k)
    d.text((x, int(18 * k)), "VICTORY!", fill=(235, 190, 70), font=_font(int(30 * k)))
    d.text((x, int(58 * k)), mode, fill=(170, 175, 180), font=_font(int(17 * k)))
    d.text((x, int(82 * k)), when, fill=(170, 175, 180), font=_font(int(15 * k)))
    d.text((int(w * 0.62), int(150 * k)), "PERSONAL   TEAM RESULT", fill=(230, 230, 230),
           font=_font(int(22 * k)))
    im.paste(pic, (bars, 0))
    im.save(path)
    return path


class TestHeaderCrop(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_crop_is_the_top_left_corner_enlarged_within_limits(self):
        for w, h in ((1920, 1080), (2560, 1440), (3840, 2160), (1280, 720)):
            p = result_screen(os.path.join(self.tmp, "s.png"), w, h)
            g = screenocr.header_crop(p)
            self.assertLessEqual(max(g.size), screenocr.MAX_DIM, (w, h))
            self.assertGreaterEqual(g.size[0], int(w * screenocr.BAND_W), (w, h))
            self.assertEqual(g.mode, "L")

    def test_ultrawide_bars_are_trimmed_first(self):
        p = result_screen(os.path.join(self.tmp, "uw.png"), 2560, 1440, bars=440)
        with Image.open(p) as im:
            from debrief_uploader import images
            x0, pw = images.pillarbox_bounds(im)
        self.assertEqual((x0, pw), (440, 2560))
        g = screenocr.header_crop(p)
        # the crop starts at the game picture, not in the black bar
        self.assertGreater(g.getpixel((5, g.size[1] // 2)), 20)

    @unittest.skipIf(sys.platform == "win32", "OCR is available on Windows")
    def test_reads_nothing_off_windows(self):
        p = result_screen(os.path.join(self.tmp, "s.png"))
        self.assertFalse(screenocr.available())
        self.assertEqual(screenocr.read_headers([p]), {})


@unittest.skipUnless(sys.platform == "win32", "Windows OCR")
class TestWindowsOcr(unittest.TestCase):
    """The real Windows.Media.Ocr pass on a drawn result-screen corner."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self, paths):
        got = screenocr.read_headers(paths)
        if not got:
            self.skipTest("Windows OCR unavailable here: %s"
                          % screenocr.why_unavailable())
        return got

    def test_reads_the_battle_time(self):
        cases = [((1920, 1080, 0), "09.11.2026 20:27"),
                 ((2560, 1440, 0), "15.10.2026 08:39"),
                 ((1280, 720, 0), "27.10.2026 19:06"),
                 ((2560, 1080, 340), "31.12.2026 23:59")]
        paths = [result_screen(os.path.join(self.tmp, "%d.png" % i), w, h, when, bars=b)
                 for i, ((w, h, b), when) in enumerate(cases)]
        got = self._read(paths)
        print("\nWindows OCR read:", {os.path.basename(p): got.get(p) for p in paths})
        for p, (_size, when) in zip(paths, cases):
            self.assertEqual(got[p][0], P(when), (p, got[p]))

    def test_a_screen_without_the_line_reads_as_none(self):
        p = os.path.join(self.tmp, "blank.png")
        Image.new("RGB", (1920, 1080), (34, 44, 54)).save(p)
        got = self._read([p, result_screen(os.path.join(self.tmp, "ok.png"))])
        self.assertEqual(got[p], (None, None))


@unittest.skipUnless(sys.platform == "win32", "Windows OCR")
class TestWindowsEndToEnd(LateSetsBase):
    """Greg's case through the engine with the REAL Windows OCR reading
    drawn result screens -- no stand-in reader."""

    def setUp(self):
        super().setUp()
        self.eng.read_screen_headers = (
            lambda paths: screenocr.read_headers(paths, self.log))

    def make_tab(self, name, mtime, when):
        p = os.path.join(self.sdir, name)
        result_screen(p, when=when.strftime("%d.%m.%Y %H:%M"))
        os.utime(p, (mtime, mtime))

    def test_each_set_goes_to_its_own_uploaded_battle(self):
        if not screenocr.read_headers([os.path.join(self.sdir, "p1.png")]):
            self.skipTest("Windows OCR unavailable here: %s"
                          % screenocr.why_unavailable())
        self.check_each_set_goes_to_its_own_uploaded_battle()


if __name__ == "__main__":
    unittest.main()
