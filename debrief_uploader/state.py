"""Local state: what we've seen, what we've matched, what we've uploaded.

SQLite because it survives a crash mid-upload, which a JSON file does not.

THREADING: the tray runs the engine on a worker thread while the menu and the
review window read counts from other threads, so the connection is shared with
check_same_thread=False and every statement is serialised behind one lock.
sqlite3 refuses cross-thread use of a connection by default -- that is what it
is protecting against, and a lock is the honest way to satisfy it. Every
operation here is a handful of small rows, so serialising them costs nothing
and removes intra-process "database is locked" entirely.
"""
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS replays (
  md5           TEXT PRIMARY KEY,
  source_path   TEXT,
  staged_path   TEXT,
  filename      TEXT,
  size          INTEGER,
  t_close       REAL,      -- the matching clock (file mtime / observed close)
  header_dt     REAL,      -- battle start per the game's own clock, may skew
  match_group   TEXT,
  player_ship   TEXT,
  player_name   TEXT,
  map_name      TEXT,
  duration_s    INTEGER,
  played_at     TEXT,      -- ISO, exactly as the browser computes it
  state         TEXT,
  short_code    TEXT,
  remote_id     TEXT,
  attempts      INTEGER DEFAULT 0,
  next_try      REAL DEFAULT 0,
  last_error    TEXT,
  note          TEXT,
  backfill      INTEGER DEFAULT 0,
  created_at    REAL,
  updated_at    REAL
);
CREATE INDEX IF NOT EXISTS replays_state ON replays(state);
CREATE INDEX IF NOT EXISTS replays_close ON replays(t_close);

CREATE TABLE IF NOT EXISTS shots (
  path        TEXT PRIMARY KEY,
  t           REAL,
  width       INTEGER,
  height      INTEGER,
  replay_md5  TEXT,
  uploaded    INTEGER DEFAULT 0,
  ignored     INTEGER DEFAULT 0,
  seen_at     REAL
);
CREATE INDEX IF NOT EXISTS shots_replay ON shots(replay_md5);
CREATE INDEX IF NOT EXISTS shots_t ON shots(t);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    # ---- serialised primitives -------------------------------------------
    def _all(self, sql, args=()):
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    def _one(self, sql, args=()):
        with self._lock:
            return self.db.execute(sql, args).fetchone()

    def _write(self, sql, args=()):
        with self._lock:
            self.db.execute(sql, args)
            self.db.commit()

    def _writemany(self, sql, rows):
        with self._lock:
            self.db.executemany(sql, rows)
            self.db.commit()

    # ---- replays ----------------------------------------------------------
    def have_source_path(self, path):
        """Cheap pre-filter so a poll doesn't re-hash the whole folder."""
        return self._one("SELECT 1 FROM replays WHERE source_path=?", (path,)) is not None

    def have_replay(self, md5):
        return self._one("SELECT 1 FROM replays WHERE md5=?", (md5,)) is not None

    def add_replay(self, **kw):
        kw.setdefault("created_at", time.time())
        kw["updated_at"] = time.time()
        cols = ",".join(kw)
        marks = ",".join("?" * len(kw))
        self._write("INSERT OR IGNORE INTO replays (%s) VALUES (%s)" % (cols, marks),
                    tuple(kw.values()))

    def update_replay(self, md5, **kw):
        if not kw:
            return
        kw["updated_at"] = time.time()
        sets = ",".join("%s=?" % k for k in kw)
        self._write("UPDATE replays SET %s WHERE md5=?" % sets,
                    tuple(kw.values()) + (md5,))

    def replay(self, md5):
        return self._one("SELECT * FROM replays WHERE md5=?", (md5,))

    def replays(self, states=None):
        if states:
            q = "SELECT * FROM replays WHERE state IN (%s) ORDER BY t_close" % \
                ",".join("?" * len(states))
            return self._all(q, tuple(states))
        return self._all("SELECT * FROM replays ORDER BY t_close")

    def recent(self, limit=20):
        return self._all(
            "SELECT * FROM replays ORDER BY COALESCE(t_close,0) DESC LIMIT ?",
            (limit,))

    # ---- shots ------------------------------------------------------------
    def have_shot(self, path):
        return self._one("SELECT 1 FROM shots WHERE path=?", (path,)) is not None

    def add_shot(self, path, t, width=None, height=None):
        self._write(
            "INSERT OR IGNORE INTO shots (path,t,width,height,seen_at) VALUES (?,?,?,?,?)",
            (path, t, width, height, time.time()))

    def free_shots(self, since=None):
        """Screenshots not yet attached to a battle and not ignored."""
        q = "SELECT * FROM shots WHERE replay_md5 IS NULL AND ignored=0"
        args = ()
        if since is not None:
            q += " AND t >= ?"
            args = (since,)
        return self._all(q + " ORDER BY t", args)

    def shots_for(self, md5):
        return self._all("SELECT * FROM shots WHERE replay_md5=? ORDER BY t", (md5,))

    def attach_shots(self, paths, md5):
        self._writemany("UPDATE shots SET replay_md5=? WHERE path=?",
                        [(md5, p) for p in paths])

    def detach_shots(self, md5):
        self._write("UPDATE shots SET replay_md5=NULL WHERE replay_md5=?", (md5,))

    def ignore_shots(self, paths):
        self._writemany("UPDATE shots SET ignored=1 WHERE path=?", [(p,) for p in paths])

    def mark_shots_uploaded(self, md5):
        self._write("UPDATE shots SET uploaded=1 WHERE replay_md5=?", (md5,))

    def all_shots(self, uploaded=None):
        if uploaded is None:
            return self._all("SELECT * FROM shots")
        return self._all("SELECT * FROM shots WHERE uploaded=?",
                         (1 if uploaded else 0,))

    def forget_shots(self, paths):
        """Detach and ignore: these are never matched or uploaded."""
        self._writemany("UPDATE shots SET replay_md5=NULL, ignored=1 WHERE path=?",
                        [(p,) for p in paths])

    def prune_shots(self, older_than):
        """Forget screenshots that never found a battle. The files are never
        touched -- only our note that we looked at them."""
        self._write("DELETE FROM shots WHERE replay_md5 IS NULL AND t < ?",
                    (older_than,))

    # ---- kv ---------------------------------------------------------------
    def get(self, k, default=None):
        r = self._one("SELECT v FROM kv WHERE k=?", (k,))
        return r["v"] if r else default

    def set(self, k, v):
        self._write("INSERT INTO kv (k,v) VALUES (?,?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
