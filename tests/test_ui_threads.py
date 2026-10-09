"""Windows built on their own threads must not crash the app when the next
one opens.

Stargatecraft (PR #6, 2026-10-09), reliably: open Status from the tray while
signed out, close it with the title bar's X, open Settings ->
"Tcl_AsyncDelete: async handler deleted by the wrong thread" and the process
dies. Each window runs its own Tk on its own thread (pystray owns the main
one). Tk objects caught in reference CYCLES (bindings that close over their
widget, images and variables tied to the window) outlive the window and are
freed by Python's cyclic GC - on whichever thread happens to trigger it.
Freeing a Tcl interpreter from the wrong thread aborts the process.

The child below closes a window on its thread, then forces a collection on
the MAIN thread - the same wrong-thread cleanup the app hits by chance -
then opens the next window. It must exit 0.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")

try:
    import tkinter as tk
    _t = tk.Tk()
    _t.destroy()
    HAVE_TK = True
except Exception:
    HAVE_TK = False

CHILD = textwrap.dedent(r'''
    import gc, os, sys, threading, time
    sys.path.insert(0, %(root)r)
    sys.path.insert(0, os.path.join(%(root)r, "tests"))
    os.environ["GD_UPLOADER_HOME"] = %(home)r
    import test_ui_builds as T
    from debrief_uploader import config, ui
    app = T.App()
    s = config.Settings(); s.update({"replay_dirs": [], "shot_dirs": []})
    app.s, app.store, app.log, app.eng = s, T.FakeStore(), T.FakeLog(), T.FakeEngine()
    class SignedOut(T.FakeClient):
        class session:
            signed_in = False; email = None; user_id = None
    app.client = SignedOut()
    real_root = ui._root
    done = []
    def root(title, w, h):
        r = real_root(title, w, h)
        def close():
            # the title bar's X where there is one, else the window's own close
            x = None
            stack = [r]
            while stack:
                wdg = stack.pop(); stack.extend(wdg.winfo_children())
                if getattr(wdg, "cget", None) and wdg.winfo_class() == "Label":
                    try:
                        if wdg.cget("text") == "": x = wdg
                    except Exception:
                        pass
            if x is not None:
                x.event_generate("<Button-1>")
            else:
                r.tk.call(r.protocol("WM_DELETE_WINDOW") or "destroy")
            done.append(title)
        r.after(400, close)
        r.after(4000, lambda: r.winfo_exists() and r.destroy())
        return r
    ui._root = root
    for opener in (ui.open_status, ui.open_settings, ui.open_status, ui.open_doctor):
        n = len(done)
        opener(app)                      # real ui._thread: its own thread
        t0 = time.time()
        while len(done) == n and time.time() - t0 < 10:
            time.sleep(0.05)
        time.sleep(0.5)                  # let that thread finish
        gc.collect()                     # wrong-thread cleanup, on purpose
    print("survived", len(done))
''')


@unittest.skipUnless(HAVE_TK and os.name == "nt", "the crash is a Tcl abort on Windows threads")
class TestWindowThreads(unittest.TestCase):
    def test_closing_one_window_does_not_kill_the_next(self):
        home = tempfile.mkdtemp()
        code = CHILD % {"root": os.path.abspath(ROOT), "home": home}
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, "child died (rc=%s)\n%s\n%s" % (r.returncode, r.stdout[-800:], r.stderr[-1500:]))
        self.assertIn("survived 4", r.stdout)


if __name__ == "__main__":
    unittest.main()
