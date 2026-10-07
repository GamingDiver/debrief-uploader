"""Folder watching, write-settling, and quarantine.

Polling, not FileSystemWatcher/watchdog. The folders hold ~10 files and a
handful of screenshots, so a 2-second scan costs nothing, and it cannot drop
events across sleep/resume, on buffer overflow, or on a virtual volume -- which
event APIs demonstrably do. One mechanism that always works beats two that
mostly do.
"""
import hashlib
import os
import shutil
import time

from .config import is_excluded

MAGIC = b"\x57\x47\xff\x52"      # "WG\xffR"
REPLAY_EXT = ".wowsreplay"
TEMP_NAME = "temp.wowsreplay"    # written during the battle; never a candidate
SHOT_EXTS = (".png", ".jpg", ".jpeg")


def is_replay(path):
    """First four bytes must be the WoWS magic. Same guard the mac-side
    backup script uses, and the same one the edge function applies."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == MAGIC
    except OSError:
        return False


def md5_of(path, chunk=1 << 20):
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
    except OSError:
        return None
    return h.hexdigest()


def recently_written(path, mtime=None, within=120):
    """Is this file new enough that it might still be being written?

    Anything older is finished by definition, and waiting `quiet` seconds to
    confirm it -- per file, across a folder of thousands -- is how the first
    version of this scan never got past last month.
    """
    try:
        mt = mtime if mtime is not None else os.path.getmtime(path)
    except OSError:
        return False
    return (time.time() - mt) < within


def settled(path, quiet, giveup, sleep=0.5):
    """True once the file has stopped changing and nothing holds it open.

    The game writes into temp.wowsreplay during the battle and the finished
    file appears at the end, but a copy in progress (or a slow disk) can still
    show us a partial file, so wait for size+mtime to hold still.
    """
    deadline = time.time() + giveup
    last = None
    stable_since = None
    while time.time() < deadline:
        try:
            st = os.stat(path)
        except OSError:
            return False
        cur = (st.st_size, st.st_mtime)
        now = time.time()
        if cur != last:
            last, stable_since = cur, now
        elif now - stable_since >= quiet:
            return _openable(path)
        time.sleep(sleep)
    return False


def _openable(path):
    """On Windows an exclusive open fails while the game still holds the file.
    On other platforms this is just a readability check."""
    try:
        with open(path, "rb") as f:
            f.read(4)
        return True
    except OSError:
        return False


def scan_replays(dirs, exclude=None, since=None):
    """Every candidate replay path, newest first.

    Newest first matters: the game keeps only ~10, but if a scan is ever cut
    short the battle that just finished is the one that must not be missed.

    `exclude`: folders never read. `since`: files older than this epoch are
    skipped -- both before the file is opened, hashed or copied.
    """
    out = []
    for d in dirs:
        if is_excluded(d, exclude):
            continue
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.name == TEMP_NAME or not e.name.lower().endswith(REPLAY_EXT):
                continue
            try:
                if not e.is_file():
                    continue
                mt = e.stat().st_mtime
            except OSError:
                continue
            if since is not None and mt < since:
                continue
            if is_excluded(e.path, exclude):
                continue
            out.append((e.path, mt))
    out.sort(key=lambda r: r[1], reverse=True)
    return [p for p, _ in out]


def scan_shots(dirs, min_bytes=0, max_age=None, now=None, exclude=None,
               since=None):
    """Candidate screenshots, newest first, already filtered.

    The filtering happens HERE, from the directory entry's own stat data,
    because the alternative is what shipped first: settle-check and decode
    every image in the folder. A screenshots folder is a lifetime archive --
    one user had thousands, and at ~2 s each the app spent a whole night
    working through 2026-08-02 without ever reaching that evening's battle.

    os.scandir carries stat data from the directory read on Windows, so age
    and size cost nothing extra.

    Returns [(path, mtime, size)], newest first.
    """
    now = now if now is not None else time.time()
    out = []
    for d in dirs:
        if is_excluded(d, exclude):
            continue
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if not e.name.lower().endswith(SHOT_EXTS):
                continue
            try:
                if not e.is_file():
                    continue
                st = e.stat()
            except OSError:
                continue
            if max_age is not None and now - st.st_mtime > max_age:
                continue
            if st.st_size < min_bytes:
                continue
            if since is not None and st.st_mtime < since:
                continue
            if is_excluded(e.path, exclude):
                continue
            out.append((e.path, st.st_mtime, st.st_size))
    out.sort(key=lambda r: r[1], reverse=True)
    return out


def quarantine(path, md5, staging):
    """Copy a replay somewhere the game's ~10-file rotation cannot reach.

    This happens BEFORE any matching or network work: the whole point is that
    a replay we have seen can never be destroyed by the next battle. mtime is
    preserved because it is the matching clock.
    """
    os.makedirs(staging, exist_ok=True)
    dest = os.path.join(staging, md5 + REPLAY_EXT)
    if os.path.exists(dest):
        return dest
    tmp = dest + ".part"
    shutil.copy2(path, tmp)
    os.replace(tmp, dest)
    return dest


def close_time(path):
    """The matching clock: the file's mtime, always.

    An earlier draft preferred "the moment the watcher saw the file appear" for
    live files. That is strictly worse -- it is mtime plus however long the poll
    and settle took -- and it makes the same battle carry a different time
    depending on whether the app happened to be running. mtime is the true
    close time, it is stable across restarts, and it is the clock the archive
    analysis validated.

    Never the filename timestamp: 36% of the measured archive has the game's
    clock a whole -9h from the filesystem clock.
    """
    try:
        return os.path.getmtime(path)
    except OSError:
        return time.time()
