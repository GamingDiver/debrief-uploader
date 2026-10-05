"""The matching engine: screenshots -> clusters -> the battle they belong to.

Pure logic. No file I/O, no network, no clock of its own -- every function
takes the times it needs. That is deliberate: this is the only part of the app
that can silently corrupt research data, so it has to be exhaustively testable
without a game, a PC, or a network.

Two rules, measured on a real archive of ~500 battles, drive everything here:

1. Match on file MTIME, never the filename timestamp. 174 of 483 archived
   replays show a whole -9h skew between the game's clock (which names the
   file) and the filesystem clock (which stamps mtime). mtime and a
   screenshot's write time come from the same OS clock; the filename does not.

2. "Nearest preceding replay" is the primary rule and the time window is only
   a guard: 5% of consecutive battles close within 4 minutes of each other, so
   any window wide enough to cover a delayed results screen (~25 min) spans two
   or three replays.
"""
from dataclasses import dataclass, field
from typing import List, Optional

# ---- replay lifecycle ------------------------------------------------------
NEW = "new"                      # quarantined, metadata not read yet
AWAITING_SHOTS = "awaiting_shots"  # metadata read, real battle, needs scorecards
HELD = "held"                    # matched but a human should confirm
READY = "ready"                  # matched (or exempt), queued to upload
UPLOADING = "uploading"
UPLOADED = "uploaded"
ORPHAN = "orphan"                # gave up waiting for scorecards
FAILED = "failed"
SKIPPED = "skipped"

# States a cluster may attach to. A replay that is already uploading, uploaded,
# held or orphaned is not up for grabs (spec condition B).
PAIRABLE = (AWAITING_SHOTS,)

TRAINING = "training_room"


@dataclass
class ReplayRec:
    """What the matcher needs to know about one replay."""
    key: str                       # content md5
    t_close: float                 # epoch seconds -- THE matching clock
    state: str = NEW
    match_group: Optional[str] = None   # None until replay-meta has been read
    ship: Optional[str] = None
    map_name: Optional[str] = None
    header_dt: Optional[float] = None   # epoch from the header, may be skewed
    duration: Optional[float] = None    # the battle's TIME LIMIT, not its length

    @property
    def is_training(self):
        return (self.match_group or "").lower() == TRAINING


@dataclass
class ShotRec:
    path: str
    t: float                       # epoch seconds, mtime or observed arrival


@dataclass
class Cluster:
    shots: List[ShotRec] = field(default_factory=list)

    @property
    def t_first(self):
        return min(s.t for s in self.shots)

    @property
    def t_last(self):
        return max(s.t for s in self.shots)

    def __len__(self):
        return len(self.shots)


# ---- decisions -------------------------------------------------------------
AUTO = "auto"        # confident: upload it
HOLD = "hold"        # ambiguous: ask a human, never guess
NONE = "none"        # nothing to attach to (yet)


@dataclass
class Decision:
    kind: str
    replay: Optional[ReplayRec] = None
    alternatives: List[ReplayRec] = field(default_factory=list)
    reason: str = ""

    def __repr__(self):
        k = self.replay.key[:8] if self.replay else "-"
        return "Decision(%s, %s, %r)" % (self.kind, k, self.reason)


# ---- clustering ------------------------------------------------------------

def settle_for(cluster, pair_settle, pair_settle_full):
    """How long this group waits before it is decided.

    A group holding two or more screenshots already looks like a finished
    scorecard capture (Personal, then Team Result), so it is decided quickly.
    A lone screenshot waits the full time, because the partner tab is exactly
    what is worth waiting for.
    """
    if pair_settle_full is None:
        return pair_settle
    return pair_settle_full if len(cluster) >= 2 else pair_settle


def cluster_shots(shots, pair_gap, pair_settle, now, pair_settle_full=None):
    """Group screenshots into per-battle clusters.

    A player takes the Personal shot, flips the tab, takes the Team Result
    shot -- seconds apart. So: a new screenshot opens a cluster, and any
    further shot within `pair_gap` of the PREVIOUS one joins it.

    Returns (closed, open_) -- only closed clusters are ready to match; an open
    one may still gain another screenshot.
    """
    groups = []
    for s in sorted(shots, key=lambda s: s.t):
        if groups and s.t - groups[-1].shots[-1].t <= pair_gap:
            groups[-1].shots.append(s)
        else:
            groups.append(Cluster([s]))
    closed, open_ = [], []
    for g in groups:
        wait = settle_for(g, pair_settle, pair_settle_full)
        (closed if now - g.t_last >= wait else open_).append(g)
    return closed, open_


def decides_at(cluster, pair_settle, pair_settle_full=None):
    """When an open group will be decided -- so waiting can be reported
    instead of looking like nothing happening."""
    return cluster.t_last + settle_for(cluster, pair_settle, pair_settle_full)


# ---- matching --------------------------------------------------------------

def match_cluster(cluster, replays, cfg):
    """Decide which battle a closed cluster of screenshots belongs to.

    cfg needs: grace, max_lag, max_shots, review_mode.
    `replays` may be in any order and may contain any state.
    """
    t0 = cluster.t_first

    if len(cluster) > cfg["max_shots"]:
        return Decision(HOLD, reason="%d screenshots in one burst - pick the scorecards"
                                     % len(cluster))

    # Candidates: replays that had closed by the time the shots were taken.
    # `grace` allows a shot snapped just before the file finished closing.
    cands = [r for r in replays if r.state in PAIRABLE and r.t_close <= t0 + cfg["grace"]]
    if not cands:
        return Decision(NONE, reason="no battle had finished when these were taken")

    best = max(cands, key=lambda r: r.t_close)

    # A training room has no post-battle scorecard screens at all, so these
    # shots are not of it -- and they are not of the battle before it either.
    # Refuse rather than skip past it onto an older replay.
    if best.is_training:
        return Decision(NONE, reason="the last battle was a training room, which has no scorecards")

    lag = t0 - best.t_close
    if lag > cfg["max_lag"]:
        return Decision(NONE, reason="nearest battle finished %d min earlier" % (lag // 60))

    # --- condition C: the delayed-results trap ------------------------------
    # The danger is not "which replay is nearest". It is that the player left
    # battle A early, queued battle B, and A's results screen only appeared
    # after B's replay had already closed -- so A's scorecards sit after B.
    # In a healthy session every replay gets its shots, so nothing older is
    # ever left unpaired and this never fires. It fires exactly when the user
    # skipped a battle's screenshots, which is exactly when misattribution is
    # possible.
    # An older battle only counts as a rival claimant if its results screen
    # could plausibly still have been up when these were taken. Without that
    # check, ONE battle the player never screenshotted holds up every battle
    # after it, forever -- which is what happened on a real machine: a 08:56
    # game that ended by 09:11 at the latest was still blocking 09:26
    # screenshots that obviously belonged to the 09:12 game.
    older = [r for r in cands
             if r.key != best.key and r.t_close < best.t_close
             and t0 - r.t_close <= cfg["max_lag"]
             and not r.is_training
             and could_still_be_showing(r, t0, cfg)]
    if older:
        older.sort(key=lambda r: r.t_close, reverse=True)
        return Decision(HOLD, replay=best, alternatives=older,
                        reason="an earlier battle is still missing its scorecards")

    if len(cluster) < 2:
        return Decision(HOLD, replay=best,
                        reason="only one scorecard - the other tab is missing")

    if cfg.get("review_mode"):
        return Decision(HOLD, replay=best, reason="review mode is on")

    return Decision(AUTO, replay=best, reason="matched %ds after the battle closed" % int(lag))


def results_window_end(r, grace):
    """The last moment this battle's results screen could still be on screen.

    A replay file closes when the player LEAVES the battle, which for an early
    death is long before the battle actually ends -- and the results screen
    only appears at the real end. So a battle someone left at 0:18 can still
    produce scorecards fifteen minutes later.

    elapsed = t_close - header_dt is how much of the battle had been played
    when the file closed, so `duration - elapsed` is what was left to run.
    Both timestamps must be on the same clock for that subtraction to mean
    anything, which is exactly what skew_is_sane checks -- 36% of one measured
    archive had the game clock a whole -9h from the filesystem clock.

    Returns None when it cannot be known, and callers must stay conservative.
    """
    elapsed = clock_skew(r)
    if not skew_is_sane(elapsed) or not r.duration:
        return None
    remaining = max(0.0, r.duration - elapsed)
    return r.t_close + remaining + grace


def could_still_be_showing(r, t0, cfg):
    """Could these screenshots be of THIS battle's results screen?

    True when unknowable: never let a missing timestamp turn into a confident
    wrong answer.
    """
    end = results_window_end(r, cfg.get("results_grace", 300))
    return end is None or t0 <= end


def is_orphan(replay, replays, now, cfg):
    """A replay that waited for scorecards and never got them.

    Two triggers, whichever comes first: a wall-clock timeout, or enough later
    battles having finished that the player has clearly moved on.
    """
    if replay.state not in PAIRABLE:
        return False
    if now - replay.t_close >= cfg["orphan_after"]:
        return True
    later = sum(1 for r in replays if r.t_close > replay.t_close)
    return later >= cfg["orphan_after_n_later"]


def clock_skew(replay):
    """Sanity check, never a gate. Expect the header's battle-start to sit
    0..1200s before the file close. Anything else means the game clock and the
    filesystem clock disagree (the measured -9h case), so the UI must stop
    showing the filename time on that machine. Returns None if unknowable."""
    if replay.header_dt is None:
        return None
    return replay.t_close - replay.header_dt


def skew_is_sane(skew):
    return skew is not None and 0 <= skew <= 1200
