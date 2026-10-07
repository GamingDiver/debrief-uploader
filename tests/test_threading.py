"""The store and the engine are driven from more than one thread.

The tray runs the engine on a worker thread while the menu and the review
window read counts from others. sqlite3 refuses cross-thread use of a
connection by default, which took down the tray worker on first contact with a
real PC -- silently, since a dead worker still leaves the icon sitting there.
These tests reproduce that shape.
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from debrief_uploader.state import Store


class TestStoreThreading(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "state.db"))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_from_another_thread(self):
        """The exact failure seen on Windows: created on the main thread,
        read from the tray worker."""
        self.store.add_replay(md5="a", state="held", t_close=1.0)
        err = []

        def worker():
            try:
                self.store.replays(["held"])
                self.store.recent(5)
                self.store.get("clock_skew_hours")
            except Exception as e:
                err.append(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertEqual(err, [], "store must be readable from any thread")

    def test_write_from_another_thread(self):
        err = []

        def worker(n):
            try:
                for i in range(20):
                    self.store.add_replay(md5="%d-%d" % (n, i), state="new",
                                          t_close=float(i))
                    self.store.update_replay("%d-%d" % (n, i), state="ready")
                    self.store.add_shot("/s/%d-%d.png" % (n, i), float(i))
            except Exception as e:
                err.append(e)

        ts = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(err, [], "concurrent writes must not raise")
        self.assertEqual(len(self.store.replays(["ready"])), 80)

    def test_reads_and_writes_interleaved(self):
        """A writer thread (the engine) and a reader thread (the tray menu)
        running at the same time, which is the steady state of the app."""
        stop = threading.Event()
        err = []

        def writer():
            i = 0
            while not stop.is_set():
                try:
                    self.store.add_replay(md5="w%d" % i, state="new", t_close=1.0)
                    i += 1
                except Exception as e:
                    err.append(e)
                    return

        def reader():
            while not stop.is_set():
                try:
                    self.store.replays(["new"])
                    self.store.replays(["held"])
                except Exception as e:
                    err.append(e)
                    return

        w, r = threading.Thread(target=writer), threading.Thread(target=reader)
        w.start()
        r.start()
        time.sleep(0.4)
        stop.set()
        w.join()
        r.join()
        self.assertEqual(err, [])


class TestEngineOnWorkerThread(unittest.TestCase):
    """A full engine tick from a thread that did not create the store --
    the tray's arrangement."""

    def test_tick_from_worker_thread(self):
        from debrief_uploader import config, engine, watcher
        from debrief_uploader.log import Log
        from tests.test_engine import FakeClient

        tmp = tempfile.mkdtemp()
        rdir = os.path.join(tmp, "replays")
        os.makedirs(rdir)
        with open(os.path.join(rdir, "a.wowsreplay"), "wb") as f:
            f.write(watcher.MAGIC + os.urandom(2048))

        orig = config.staging_dir
        config.staging_dir = lambda: os.path.join(tmp, "staging")
        try:
            s = config.Settings()
            s["watching_confirmed"] = True
            s.update({"replay_dirs": [rdir], "shot_dirs": [],
                      "settle_quiet": 0.01, "settle_sleep": 0.01,
                      "min_upload_gap": 0, "max_uploads_per_hour": 0})
            store = Store(os.path.join(tmp, "state.db"))
            eng = engine.Engine(s, store, FakeClient(), Log(None, echo=False))
            eng._first_scan = False
            eng._watch_start = lambda path: 0
            err = []

            def worker():
                try:
                    for _ in range(3):
                        eng.tick()
                except Exception as e:
                    err.append(e)

            t = threading.Thread(target=worker)
            t.start()
            t.join()
            self.assertEqual(err, [], "the tray worker must survive a full tick")
            self.assertEqual(len(store.replays()), 1)
            store.close()
        finally:
            config.staging_dir = orig
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
