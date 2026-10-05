"""The orchestrator: intake -> metadata -> match -> queue.

One tick does a bounded amount of work and returns, so the caller can run it
from a plain loop or a tray timer. Nothing here blocks for longer than a single
upload.
"""
import os
import time

from . import api, config, images, matcher, uploader, watcher
from .api import ApiError
from .matcher import (AWAITING_SHOTS, HELD, NEW, ORPHAN, READY, ReplayRec,
                      ShotRec, SKIPPED, UPLOADED, UPLOADING)


def _rec(row):
    return ReplayRec(key=row["md5"], t_close=row["t_close"] or 0,
                     state=row["state"], match_group=row["match_group"],
                     ship=row["player_ship"], map_name=row["map_name"],
                     header_dt=row["header_dt"], duration=row["duration_s"])


class Engine:
    def __init__(self, settings, store, client, log):
        self.s = settings
        self.store = store
        self.client = client
        self.log = log
        self.pacer = uploader.Pacer(settings["min_upload_gap"],
                                    settings["max_uploads_per_hour"])
        self.blocked = None          # terminal error message; queue is parked
        self.started = time.time()
        self._first_scan = True
        self._warned_skew = False
        self._seen_files = set()
        self._said = {}
        self._last_beat = 0.0
        self._forget_stale_shots()
        self._recheck_held()

    def _recheck_held(self):
        """Release holds that only existed because of the older rule.

        Until v1.0.7 any unpaired earlier battle blocked a match, whether or
        not its results could still have been on screen -- so one battle a
        player never screenshotted held up every battle after it, and the
        count only grew. Those holds were wrong, and clearing them by hand is
        not the user's job.

        Only ambiguity holds are re-examined. A backfill hold ("found before
        the app was running") is held for a different and still-valid reason
        and is left alone.
        """
        released = 0
        for row in self.store.replays([HELD]):
            if row["backfill"]:
                continue
            shots = self.store.shots_for(row["md5"])
            if len(shots) < 2:
                continue
            t0 = min(s["t"] for s in shots)
            rec = _rec(row)
            rivals = [
                _rec(o) for o in self.store.replays([AWAITING_SHOTS, HELD])
                if o["md5"] != row["md5"] and (o["t_close"] or 0) < rec.t_close
                and t0 - (o["t_close"] or 0) <= self.s["max_lag"]
            ]
            rivals = [r for r in rivals
                      if not r.is_training
                      and matcher.could_still_be_showing(r, t0, self.s)]
            if rivals:
                continue
            self.store.update_replay(row["md5"], state=READY, note=None)
            self.log.info("no longer ambiguous, queuing %s" % self._label(row))
            released += 1
        return released

    def _forget_stale_shots(self):
        """Drop remembered screenshots older than the window we now consider.

        An earlier build ingested a whole screenshots archive. Those rows do
        nothing but slow every match, and forgetting one has no cost: the
        file itself is never touched, and it would be re-read if it ever
        mattered again.
        """
        cutoff = time.time() - self.s["shot_max_age_hours"] * 3600
        before = len(self.store.free_shots())
        if not before:
            return
        self.store.prune_shots(cutoff)
        dropped = before - len(self.store.free_shots())
        if dropped:
            self.log.info("forgot %d screenshot(s) older than %d hours "
                          "(the files are untouched)"
                          % (dropped, self.s["shot_max_age_hours"]))

    # ---- intake -----------------------------------------------------------
    def intake_replays(self, now=None):
        now = now if now is not None else time.time()
        found = 0
        for p in watcher.scan_replays(self.s["replay_dirs"]):
            # Cheap first: settling sleeps, and hashing reads megabytes. Neither
            # should happen again for a file we already know about.
            try:
                st = os.stat(p)
            except OSError:
                continue
            fp = (p, st.st_mtime, st.st_size)
            if fp in self._seen_files:
                continue
            if self.store.have_source_path(p):
                self._seen_files.add(fp)
                continue
            if watcher.recently_written(p, st.st_mtime) and not watcher.settled(
                    p, self.s["settle_quiet"], self.s["settle_giveup"],
                    self.s["settle_sleep"]):
                continue
            if not watcher.is_replay(p):
                continue
            md5 = watcher.md5_of(p)
            if not md5 or self.store.have_replay(md5):
                self._seen_files.add(fp)
                continue
            # Quarantine FIRST: from here on the game's rotation cannot take it.
            try:
                staged = watcher.quarantine(p, md5, config.staging_dir())
            except OSError as e:
                self.log.error("could not stage %s: %s" % (os.path.basename(p), e))
                continue
            t_close = watcher.close_time(p)
            backfill = self._first_scan and (now - t_close) > self.s["max_lag"]
            self.store.add_replay(
                md5=md5, source_path=p, staged_path=staged,
                filename=os.path.basename(p), size=os.path.getsize(p),
                t_close=t_close, state=NEW, backfill=1 if backfill else 0,
                note="found before the app was running" if backfill else None)
            self._seen_files.add(fp)
            self.log.info("new replay: %s%s" % (os.path.basename(p),
                                                " (backfill)" if backfill else ""))
            found += 1
        return found

    def intake_shots(self, now=None):
        """Pick up new screenshots -- newest first, and bounded.

        Everything expensive (settle wait, image decode) happens only after
        the cheap filters in scan_shots have already ruled a file in. The
        first version did the opposite and could spend a whole night walking
        an archive of thousands without reaching the current battle.
        """
        now = now if now is not None else time.time()
        max_age = self.s["shot_max_age_hours"] * 3600
        min_bytes = self.s["shot_min_kb"] * 1024
        found = 0
        for p, mtime, size in watcher.scan_shots(self.s["shot_dirs"], min_bytes,
                                                 max_age, now):
            if found >= self.s["shot_scan_limit"]:
                break                      # the rest keep until the next pass
            if self.store.have_shot(p):
                continue
            if watcher.recently_written(p, mtime) and not watcher.settled(
                    p, self.s["settle_quiet"], 20, self.s["settle_sleep"]):
                continue
            if not images.looks_like_screenshot(p):
                # Record the REAL mtime, not 0: prune_shots ages rows out, and
                # a row stamped 0 would be pruned every pass and re-read every
                # pass, forever.
                self.store.add_shot(p, mtime)
                self.store.ignore_shots([p])
                self.log.info("ignoring %s - too small to be a scorecard"
                              % os.path.basename(p))
                continue
            try:
                from PIL import Image
                with Image.open(p) as im:
                    w, h = im.size
            except Exception:
                w = h = None
            self.store.add_shot(p, mtime, w, h)
            self.log.info("new screenshot: %s (%sx%s)"
                          % (os.path.basename(p), w, h))
            found += 1
        return found

    # ---- metadata ---------------------------------------------------------
    def read_metadata(self, limit=3):
        """Ask the site what each new replay actually is.

        This needs auth, and a replay that never gets read stays in NEW, which
        means it never becomes pairable, which means screenshots never attach
        to it. That chain used to be completely silent -- the app looked alive
        and did nothing. It now says so, once, loudly.
        """
        rows = [r for r in self.store.replays([NEW])][:limit]
        if rows:
            self.log.info("asking the site about %d replay(s)..." % len(rows))
        for r in rows:
            try:
                # Say this BEFORE the request: it uploads 1-2 MB and waits on
                # the server, and with no line here a slow connection is
                # indistinguishable from a hang.
                self.log.info("  reading %s (%.1f MB)"
                              % (r["filename"], (r["size"] or 0) / 1e6))
                meta = uploader.read_meta(self.client, r["staged_path"])
            except ApiError as e:
                if e.code == "not_authenticated":
                    self._say_once("auth", "signed out, so battles can't be "
                                   "identified and nothing will upload. Run "
                                   "'Debrief.cmd login --email'.", error=True)
                    return 0
                if e.terminal:
                    self.store.update_replay(r["md5"], state=SKIPPED,
                                             last_error=e.message)
                    self.log.warn("%s: %s" % (r["filename"], e.message))
                    continue
                self.log.warn("could not read %s yet (%s)" % (r["filename"], e.message))
                continue
            hdr = api.iso_utc(meta.get("dateTime"))
            hdr_epoch = None
            if hdr:
                hdr_epoch = time.mktime(time.strptime(hdr, "%Y-%m-%dT%H:%M:%SZ"))
            group = (meta.get("matchGroup") or "").lower()
            training = group == matcher.TRAINING
            self.store.update_replay(
                r["md5"], header_dt=hdr_epoch, match_group=group,
                player_ship=meta.get("playerVehicle"),
                player_name=meta.get("playerName"),
                map_name=meta.get("mapName"), duration_s=meta.get("duration"),
                played_at=hdr,
                state=self._state_after_meta(r, training))
            self._check_skew(_rec(self.store.replay(r["md5"])))
            self.log.info("%s: %s on %s%s" % (
                r["filename"], meta.get("playerVehicle"), meta.get("mapName"),
                " (training room - no scorecards needed)" if training else ""))
        return len(rows)

    def _say_once(self, key, msg, error=False):
        """Say something the user needs to hear, without repeating it every
        two seconds for the rest of the session."""
        if self._said.get(key):
            return
        self._said[key] = True
        (self.log.error if error else self.log.warn)(msg)

    def _state_after_meta(self, row, training):
        """A training room has no scorecard screens at all, so it never waits
        for any -- it is ready the moment we know what it is. Anything found
        before the app was running is pairable but never auto-uploaded: the
        file times are still trustworthy, the user's memory of that session
        is not.
        """
        if row["backfill"]:
            return HELD if training else AWAITING_SHOTS
        return READY if training else AWAITING_SHOTS

    def _check_skew(self, rec):
        sk = matcher.clock_skew(rec)
        if sk is not None and not matcher.skew_is_sane(sk) and not self._warned_skew:
            self._warned_skew = True
            self.store.set("clock_skew_hours", round(sk / 3600.0, 2))
            self.log.warn(
                "this PC's game clock and file clock disagree by about %.0f h - "
                "pairing uses file times, so matching is unaffected, but battle "
                "times shown from the filename would be wrong" % (sk / 3600.0))

    # ---- matching ---------------------------------------------------------
    def match(self, now=None):
        """Attach closed screenshot clusters to battles, oldest first.

        Oldest-first matters: pairing an early cluster clears that battle, so
        the next cluster is not held merely because an already-resolved battle
        looked unpaired.
        """
        now = now if now is not None else time.time()
        rows = self.store.free_shots()
        if not rows:
            return 0
        shots = [ShotRec(r["path"], r["t"]) for r in rows]
        closed, open_ = matcher.cluster_shots(
            shots, self.s["pair_gap"], self.s["pair_settle"], now,
            self.s["pair_settle_full"])
        for g in open_:
            # A silent wait is indistinguishable from a stall. Say once, per
            # group, exactly when the decision lands.
            when = matcher.decides_at(g, self.s["pair_settle"],
                                      self.s["pair_settle_full"])
            self._say_once(
                "settle:%s" % g.t_last,
                "%d screenshot(s) held until %s in case you take another"
                % (len(g), time.strftime("%H:%M:%S", time.localtime(when))))
        done = 0
        for cluster in sorted(closed, key=lambda c: c.t_first):
            live = {r["md5"]: r for r in self.store.replays()}
            recs = [_rec(r) for r in live.values()]
            d = matcher.match_cluster(cluster, recs, self.s)
            paths = [s.path for s in cluster.shots]
            if d.kind == matcher.AUTO:
                row = live[d.replay.key]
                self.store.attach_shots(paths, d.replay.key)
                if row["backfill"]:
                    self.store.update_replay(
                        d.replay.key, state=HELD,
                        note="found before the app was running - upload it?")
                    self.log.info("paired %d screenshots with %s (from before "
                                  "the app started - confirm to upload)"
                                  % (len(paths), self._label(row)))
                else:
                    self.store.update_replay(d.replay.key, state=READY, note=None)
                    self.log.info("paired %d screenshots with %s (%s)"
                                  % (len(paths), self._label(row), d.reason))
                done += 1
            elif d.kind == matcher.HOLD:
                if d.replay:
                    self.store.attach_shots(paths, d.replay.key)
                    alts = ", ".join(self._label(live[a.key]) for a in d.alternatives)
                    self.store.update_replay(
                        d.replay.key, state=HELD,
                        note=d.reason + (" (or: %s)" % alts if alts else ""))
                    self.log.warn("needs you: %s - %s. Nothing uploads for "
                                  "this battle until you run "
                                  "'Debrief.cmd review'"
                                  % (self._label(live[d.replay.key]), d.reason))
                    done += 1
        # Screenshots that never found a battle stop being considered, but the
        # files themselves are never touched.
        self.store.prune_shots(now - self.s["unmatched_keep"])
        return done

    def _label(self, row):
        ship = (row["player_ship"] or "").split("-", 1)[-1].replace("-", " ")
        parts = [p for p in (ship or None, row["map_name"] or None) if p]
        return " / ".join(parts) or (row["filename"] or row["md5"][:8])

    # ---- orphans ----------------------------------------------------------
    def sweep_orphans(self, now=None):
        now = now if now is not None else time.time()
        recs = [_rec(r) for r in self.store.replays()]
        n = 0
        for r in recs:
            if matcher.is_orphan(r, recs, now, self.s):
                self.store.update_replay(r.key, state=ORPHAN,
                                         note="no scorecards were taken")
                row = self.store.replay(r.key)
                self.log.info("no scorecards for %s - %s" % (
                    self._label(row),
                    "uploading without them" if self.s.get("upload_without_scorecards")
                    else "kept, not uploaded"))
                n += 1
        return n

    # ---- queue ------------------------------------------------------------
    def process_queue(self, now=None):
        """Upload at most one replay per tick."""
        now = now if now is not None else time.time()
        if self.blocked:
            return 0
        wait = self.pacer.wait_needed(now)
        if wait > 0:
            return 0

        row = self._next_upload(now)
        if row is None:
            return 0

        md5 = row["md5"]
        label = self._label(row)
        try:
            self.store.update_replay(md5, state=UPLOADING)
            if row["remote_id"]:
                code = uploader.resume_shots(self.client, self.store, row,
                                             self.log, self.s)
                self.log.info("Ready for review: %s  %s/wowslegends/replays/?r=%s"
                              % (label, config.SITE_BASE, row["short_code"]))
            else:
                code = uploader.upload(self.client, self.store, row, self.log, self.s)
                self.log.info("Ready for review: %s  %s/wowslegends/replays/?r=%s"
                              % (label, config.SITE_BASE, code))
            self.pacer.note(now)
            self._maybe_delete_staged(md5)
            return 1
        except ApiError as e:
            attempts = (row["attempts"] or 0) + 1
            # Re-read: upload() records remote_id as soon as the row exists, and
            # that is what decides whether the retry resumes the screenshots or
            # starts over. Using the pre-upload snapshot here would mislabel a
            # half-finished upload as never-started.
            cur = self.store.replay(md5)
            resume = bool(cur and cur["remote_id"])
            state = UPLOADING if resume else READY
            if e.terminal:
                self.blocked = e.message
                self.store.update_replay(md5, state=state, attempts=attempts,
                                         last_error=e.message)
                self.log.error("stopped: %s" % e.message)
            else:
                delay = uploader.backoff(attempts)
                self.store.update_replay(md5, state=state, attempts=attempts,
                                         last_error=e.message,
                                         next_try=now + delay)
                self.log.warn("%s failed (%s) - retrying in %ds"
                              % (label, e.message, delay))
            return 0

    def _next_upload(self, now):
        for r in self.store.replays([READY, UPLOADING]):
            if (r["next_try"] or 0) > now:
                continue
            if r["state"] == UPLOADING and not r["remote_id"]:
                continue          # interrupted before the row existed; retry as READY
            return r
        # Then battles that never got scorecards (Greg 2026-09-26: upload any
        # replay at least 15 minutes old, screenshots or not). After the
        # scorecard queue, so a matched battle is never held up behind them.
        if self.s.get("upload_without_scorecards"):
            for r in self.store.replays([ORPHAN]):
                if (r["next_try"] or 0) > now:
                    continue
                if now - (r["t_close"] or now) < self.s["no_scorecard_min_age"]:
                    continue
                return r
        return None

    def _maybe_delete_staged(self, md5):
        if not self.s.get("delete_staged_after_upload"):
            return
        row = self.store.replay(md5)
        try:
            if row["staged_path"] and os.path.exists(row["staged_path"]):
                os.remove(row["staged_path"])
                self.store.update_replay(md5, staged_path=None)
        except OSError:
            pass

    # ---- user actions -----------------------------------------------------
    def confirm(self, md5):
        """Resolve a held item: this battle really does own these screenshots."""
        self.store.update_replay(md5, state=READY, note=None)

    def reassign(self, md5, to_md5):
        shots = [s["path"] for s in self.store.shots_for(md5)]
        self.store.detach_shots(md5)
        self.store.attach_shots(shots, to_md5)
        self.store.update_replay(md5, state=AWAITING_SHOTS, note=None)
        self.store.update_replay(to_md5, state=READY, note=None)

    def skip(self, md5):
        self.store.update_replay(md5, state=SKIPPED, note="skipped by you")

    def unblock(self):
        self.blocked = None

    # ---- one pass ---------------------------------------------------------
    def tick(self, now=None):
        now = now if now is not None else time.time()
        self.intake_replays(now)
        self.intake_shots(now)
        if self.client.session.signed_in:
            self.read_metadata()
        elif self.store.replays([NEW]):
            self._say_once("signedout", "not signed in, so nothing can be "
                           "identified or uploaded. Run "
                           "'Debrief.cmd login --email'.", error=True)
        self.match(now)
        self.sweep_orphans(now)
        n = self.process_queue(now)
        self._heartbeat(now)
        self._first_scan = False
        return n

    HEARTBEAT = 300

    def _heartbeat(self, now):
        """A line every few minutes saying what it is holding.

        Without this, a healthy idle app and a wedged one look identical from
        the console -- both print nothing.
        """
        if now - self._last_beat < self.HEARTBEAT:
            return
        self._last_beat = now
        counts = self.counts()
        pending = (counts["new"] + counts["awaiting_shots"] + counts["held"]
                   + counts["ready"] + counts["uploading"])
        if not pending and not counts["uploaded"]:
            return
        bits = []
        for state, word in ((NEW, "being identified"),
                            (AWAITING_SHOTS, "waiting for scorecards"),
                            (HELD, "need you (run 'Debrief.cmd review')"),
                            (READY, "queued"), (UPLOADING, "uploading")):
            if counts[state]:
                bits.append("%d %s" % (counts[state], word))
        free = self.store.free_shots()
        if free:
            shots = [ShotRec(r["path"], r["t"]) for r in free]
            _closed, open_ = matcher.cluster_shots(
                shots, self.s["pair_gap"], self.s["pair_settle"], now,
                self.s["pair_settle_full"])
            waiting = sum(len(g) for g in open_)
            if waiting:
                bits.append("%d screenshot(s) still settling" % waiting)
            if len(free) - waiting:
                bits.append("%d unmatched screenshot(s)" % (len(free) - waiting))
        if bits:
            self.log.info("holding: " + ", ".join(bits))

    def counts(self):
        out = {}
        for r in self.store.replays():
            out[r["state"]] = out.get(r["state"], 0) + 1
        for k in (NEW, AWAITING_SHOTS, HELD, READY, UPLOADING, UPLOADED,
                  ORPHAN, SKIPPED):
            out.setdefault(k, 0)
        return out

    # ---- diagnosis --------------------------------------------------------
    def diagnose(self, now=None):
        """Everything needed to work out why nothing is happening, in one
        place. Written for pasting into a chat window."""
        now = now if now is not None else time.time()
        out = []
        add = out.append

        sess = self.client.session
        add("ACCOUNT")
        if not sess.signed_in:
            add("  NOT SIGNED IN  ->  run: Debrief.cmd login --email")
        else:
            add("  signed in as %s" % (sess.email or sess.user_id))
            left = sess.expires_at - now
            add("  access token %s"
                % ("valid for %d min" % (left / 60) if left > 0
                   else "expired (it refreshes automatically)"))
        if self.blocked:
            add("  QUEUE PARKED: %s" % self.blocked)

        add("")
        add("FOLDERS")
        for kind, dirs, exts in (("replays", self.s["replay_dirs"], (".wowsreplay",)),
                                 ("screenshots", self.s["shot_dirs"],
                                  (".png", ".jpg", ".jpeg"))):
            if not dirs:
                add("  %s: NONE CONFIGURED  ->  run: Debrief.cmd setup" % kind)
                continue
            for d in dirs:
                if not os.path.isdir(d):
                    add("  %s: MISSING  %s" % (kind, d))
                    continue
                try:
                    names = [n for n in os.listdir(d)
                             if n.lower().endswith(exts)
                             and n != watcher.TEMP_NAME]
                except OSError as e:
                    add("  %s: UNREADABLE (%s)  %s" % (kind, e, d))
                    continue
                newest = ""
                if names:
                    p = max((os.path.join(d, n) for n in names),
                            key=lambda x: os.path.getmtime(x))
                    newest = "  newest: %s at %s" % (
                        os.path.basename(p),
                        time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(p))))
                add("  %s: %d file(s)  %s%s" % (kind, len(names), d, newest))

        add("")
        add("SEEN")
        c = self.counts()
        for k in sorted(c):
            if c[k]:
                add("  %-16s %d" % (k, c[k]))
        if not any(c.values()):
            add("  nothing yet")

        stuck = self.store.replays([NEW])
        if stuck:
            add("  %d replay(s) stuck at 'new' - they cannot be paired until "
                "the site identifies them (needs sign-in)." % len(stuck))

        add("")
        add("SCREENSHOTS NOT YET MATCHED")
        free = self.store.free_shots()
        if not free:
            add("  none")
        for sh in free[-6:]:
            add("  %s  %s" % (time.strftime("%H:%M:%S", time.localtime(sh["t"])),
                              os.path.basename(sh["path"])))

        add("")
        add("WHAT THE MATCHER WOULD DO RIGHT NOW")
        if not free:
            add("  nothing to match")
        else:
            shots = [ShotRec(r["path"], r["t"]) for r in free]
            closed, open_ = matcher.cluster_shots(
                shots, self.s["pair_gap"], self.s["pair_settle"], now,
                self.s["pair_settle_full"])
            for g in open_:
                when = matcher.decides_at(g, self.s["pair_settle"],
                                          self.s["pair_settle_full"])
                add("  %d shot(s) still open - decides at %s (in %ds)"
                    % (len(g), time.strftime("%H:%M:%S", time.localtime(when)),
                       max(0, when - now)))
            live = {r["md5"]: r for r in self.store.replays()}
            recs = [_rec(r) for r in live.values()]
            for cl in sorted(closed, key=lambda c: c.t_first):
                d = matcher.match_cluster(cl, recs, self.s)
                when = time.strftime("%H:%M:%S", time.localtime(cl.t_first))
                if d.replay:
                    add("  %d shot(s) from %s -> %s: %s"
                        % (len(cl), when, d.kind.upper(),
                           self._label(live[d.replay.key])))
                else:
                    add("  %d shot(s) from %s -> %s" % (len(cl), when, d.kind.upper()))
                add("      %s" % d.reason)

        add("")
        add("RECENT BATTLES")
        for r in self.store.recent(8):
            add("  %-16s %s  %s" % (r["state"], self._label(r),
                                    r["short_code"] or r["note"] or
                                    r["last_error"] or ""))

        add("")
        add("WHAT TO DO NEXT")
        todo = []
        if not sess.signed_in:
            todo.append("Sign in:            Debrief.cmd login --email")
        if self.blocked:
            todo.append("Upload is blocked:  %s" % self.blocked)
        if not self.s["shot_dirs"]:
            todo.append("Watch screenshots:  Debrief.cmd setup --shot-dir "
                        "\"%USERPROFILE%\\Pictures\\Screenshots\"")
        if c[HELD]:
            todo.append("%d battle(s) are waiting on your decision and will "
                        "NOT upload until you clear them:\n"
                        "                      Debrief.cmd review" % c[HELD])
        if c[READY] or c[UPLOADING]:
            todo.append("%d upload(s) queued - leave it running"
                        % (c[READY] + c[UPLOADING]))
        if c[UPLOADED]:
            todo.append("%d battle(s) uploaded. If you cannot see one on the "
                        "site, check you are signed in there as the SAME "
                        "account, and that its visibility is not 'private'."
                        % c[UPLOADED])
        if not todo:
            todo.append("Nothing to do - play a battle and capture both "
                        "scorecards.")
        for t in todo:
            add("  " + t)
        return out
