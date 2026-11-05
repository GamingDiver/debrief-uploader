"""The tray popup's Tk thread must be idle between right clicks.

It used to poll its request queue with root.after(40) forever. A Windows
tray running that grew 2.7 MB a minute, nonstop, to 32 GB (2026-11-05). The
host now has no timer pending while idle, and a request wakes it directly.

Not on macOS: Tk there must run on the main thread, and this host is a
Windows feature that only CI's Windows and Linux runners exercise.
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import tkinter as tk
    _t = tk.Tk()
    _t.destroy()
    HAVE_TK = sys.platform != "darwin"
except Exception:
    HAVE_TK = False


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@unittest.skipUnless(HAVE_TK, "needs Tk off the main thread (not macOS)")
class TestPopupHostIdle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from debrief_uploader import tray
        cls.host = tray._PopupHost()
        assert _wait(lambda: cls.host.ready), "popup host never became ready"
        cls.shown = []

        def show(icon, x, y):
            cls.shown.append((threading.current_thread(), icon, x, y))
            cls.host.open = False
        cls.host._show = show
        time.sleep(0.3)

    def _pending_timers(self):
        # called from this thread; tkinter marshals it onto the Tk thread
        return self.host.root.tk.call("after", "info")

    def test_no_timer_pending_while_idle(self):
        self.assertEqual(self._pending_timers(), "")
        time.sleep(0.2)
        self.assertEqual(self._pending_timers(), "")

    def test_request_opens_on_the_tk_thread_and_leaves_nothing_pending(self):
        before = len(self.shown)
        self.host.request("icon", 10, 20)
        self.assertTrue(_wait(lambda: len(self.shown) > before))
        t, icon, x, y = self.shown[-1]
        self.assertEqual((icon, x, y), ("icon", 10, 20))
        self.assertIsNot(t, threading.current_thread())
        self.assertTrue(_wait(lambda: self._pending_timers() == ""))


if __name__ == "__main__":
    unittest.main()
