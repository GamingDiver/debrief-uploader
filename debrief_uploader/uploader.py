"""The upload sequence, plus retry and pacing policy.

Order is fixed by the server's storage policy: the replays-data bucket only
accepts a screenshot under <replay_id>/ once that replay row exists.

    1. replay-meta                 (raw bytes -> battle metadata)
    2. storage: replays            (<uid>/<stamp>_<original name>)
    3. create_replay               (-> id, short_code)
    4. storage: replays-data       (<id>/shotN.jpg)

Pacing is mandatory, not polite. There is no server-side rate limit on this
path and the backend is a Free-tier instance that has been IO-starved before,
so a backfill of 200 archived replays must not be able to take the site down.
"""
import os
import time

from . import api, images, matcher
from .api import ApiError

RETRY_BASE = 30
RETRY_MAX = 30 * 60


def backoff(attempts):
    return min(RETRY_MAX, RETRY_BASE * (2 ** max(0, attempts - 1)))


class Pacer:
    """One upload at a time, a floor between them, and an hourly ceiling."""

    def __init__(self, min_gap, per_hour):
        self.min_gap = min_gap
        self.per_hour = per_hour
        self.recent = []
        self.last = 0.0

    def wait_needed(self, now=None):
        """Seconds to wait before the next upload may start. 0 = go."""
        now = now if now is not None else time.time()
        self.recent = [t for t in self.recent if now - t < 3600]
        gap = self.min_gap - (now - self.last)
        if self.per_hour and len(self.recent) >= self.per_hour:
            gap = max(gap, 3600 - (now - self.recent[0]))
        return max(0.0, gap)

    def note(self, now=None):
        now = now if now is not None else time.time()
        self.last = now
        self.recent.append(now)


def visibility_for(rec, settings):
    """Which visibility this battle uploads as.

    Training rooms get their own setting, defaulting to private: they are
    practice, and unlike a real battle they carry no scorecard to anchor the
    Base XP research, so there is nothing for the community to gain from them
    and a decent chance the uploader would rather they were not listed.

    Returns None to mean "inherit the account's site default", which is what
    create_replay does with a null.
    """
    normal = settings.get("visibility") or None
    if (rec["match_group"] or "").lower() != matcher.TRAINING:
        return normal
    # "" (or missing) means the user chose to treat them like anything else
    return settings.get("training_visibility") or normal


def read_meta(client, staged_path):
    with open(staged_path, "rb") as f:
        return client.replay_meta(f.read())


def upload(client, store, rec, log, settings):
    """Push one replay and its screenshots. Returns the short_code.

    Raises ApiError. A terminal error must stop the queue, not spin it.
    """
    md5 = rec["md5"]
    staged = rec["staged_path"]
    if not staged or not os.path.exists(staged):
        raise ApiError("missing", "the staged replay file is gone")

    # Metadata was already read once, when we worked out what this replay was
    # and whether it needed scorecards. Re-reading it here would mean a second
    # edge-function call per upload for information we already hold.
    if rec["played_at"] and rec["player_name"]:
        played_at = rec["played_at"]
        player = rec["player_name"]
        p_meta = {"map": rec["map_name"], "mode": rec["match_group"],
                  "player_name": player, "player_ship": rec["player_ship"],
                  "duration_s": rec["duration_s"], "played_at": played_at}
    else:
        meta = read_meta(client, staged)
        played_at = api.iso_utc(meta.get("dateTime"))
        player = meta.get("playerName")
        p_meta = {"map": meta.get("mapName"), "mode": meta.get("matchGroup"),
                  "player_name": player, "player_ship": meta.get("playerVehicle"),
                  "duration_s": meta.get("duration"), "played_at": played_at}

    shots = [s for s in store.shots_for(md5)]

    # 1.5 -- the browser's duplicate pre-check. If this battle is already up,
    # attach any fresh screenshots to it rather than making a second row.
    existing = None
    try:
        existing = client.find_existing(player, played_at)
    except ApiError as e:
        if e.terminal:
            raise
        log.warn("duplicate check failed (%s); uploading anyway" % e.message)

    if existing:
        log.info("already on the site as %s; attaching screenshots"
                 % existing["short_code"])
        try:
            n = _put_shots(client, existing["id"], shots, log, settings)
        except ApiError as e:
            if not getattr(e, "permission", False):
                raise
            # We can see the row (it may be public) but cannot write under it.
            # Retrying will fail identically forever, so upload our own copy
            # instead and let the server's match_key dedupe sort it out.
            log.warn("cannot attach to %s (%s); uploading a fresh copy instead"
                     % (existing["short_code"], e.message))
            existing = None
    if existing:
        if n:
            try:
                client.rpc("reprocess_own_replay",
                           {"p_short_code": existing["short_code"],
                            "p_shots_replaced": True})
            except ApiError as e:
                log.warn("could not ask for a re-read: %s" % e.message)
        store.update_replay(md5, state=matcher.UPLOADED,
                            short_code=existing["short_code"],
                            remote_id=existing["id"], last_error=None,
                            note="already uploaded; screenshots attached")
        store.mark_shots_uploaded(md5)
        return existing["short_code"]

    # 2 -- raw replay. The path must start with our uid, and the original
    # filename is kept because the decode job reads date/ship/map off it.
    uid = client.session.user_id
    name = rec["filename"] or (md5 + ".wowsreplay")
    path = "%s/%s_%s" % (uid, api.storage_stamp(), name)
    with open(staged, "rb") as f:
        raw = f.read()
    try:
        client.storage_upload("replays", path, raw, "application/octet-stream")
    except ApiError as e:
        if e.code == "conflict":
            path = "%s/%s_%s" % (uid, api.storage_stamp(time.time() + 1), name)
            client.storage_upload("replays", path, raw, "application/octet-stream")
        else:
            raise

    # 3 -- the row.
    out = client.rpc("create_replay", {
        "p_filename": name,
        "p_storage_path": path,
        "p_meta": p_meta,
        "p_visibility": visibility_for(rec, settings),
    }) or {}
    rid, code = out.get("id"), out.get("short_code")
    if not rid or not code:
        raise ApiError("create_failed", "the site did not return a replay id")

    # Record the row BEFORE the screenshots: if we die here, the next run must
    # attach shots to this row rather than create a second one.
    store.update_replay(md5, remote_id=rid, short_code=code,
                        state=matcher.UPLOADING, last_error=None)

    # 4 -- screenshots.
    _put_shots(client, rid, shots, log, settings)
    store.update_replay(md5, state=matcher.UPLOADED, last_error=None)
    store.mark_shots_uploaded(md5)
    return code


def resume_shots(client, store, rec, log, settings):
    """We created the row but died before the screenshots landed. Never re-run
    create_replay -- that would leave an orphan duplicate row."""
    rid = rec["remote_id"]
    shots = store.shots_for(rec["md5"])
    _put_shots(client, rid, shots, log, settings)
    store.update_replay(rec["md5"], state=matcher.UPLOADED, last_error=None)
    store.mark_shots_uploaded(rec["md5"])
    return rec["short_code"]


def _put_shots(client, replay_id, shots, log, settings):
    """Transform and upload each screenshot as <id>/shotN.jpg.

    Always .jpg: the server's OCR only globs shot*.jpg/jpeg/png, so anything
    else would upload cleanly and then be silently ignored.
    """
    n = 0
    for i, s in enumerate(shots[: settings["max_shots"]], start=1):
        src = s["path"]
        if not os.path.exists(src):
            log.warn("screenshot vanished before upload: %s" % os.path.basename(src))
            continue
        try:
            data, ext, info = images.transform(src)
        except Exception as e:
            log.warn("could not process %s (%s)" % (os.path.basename(src), e))
            continue
        client.storage_upload("replays-data", "%s/shot%d.%s" % (replay_id, i, ext),
                              data, "image/jpeg", upsert=True)
        n += 1
    return n
