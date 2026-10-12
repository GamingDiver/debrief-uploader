"""The Status and "Why isn't it uploading?" windows."""


from .. import config
from . import core
from .core import (
    AMBER, BG, INK, MUTED,
    PANEL, _gutter, _label, _poll,
    _text_body, _text_window)


# ---------------------------------------------------------------- status ----

def open_status(app):
    """What it has seen, and -- first -- what is ready for you to look at.

    An uploaded battle is only finished from the app's point of view; the
    thing the user actually wants is the page. So those come first, labelled,
    with the link right there and clickable.

    Left open, it keeps itself current (Greg 2026-10-05: "refresh it after
    each new event"): every REFRESH_MS it re-reads the store and redraws only
    when something changed, keeping the scroll position, so a capture,
    upload or failure shows up without reopening the window.
    """
    REFRESH_MS = 2000

    def build():
        import tkinter as tk
        import webbrowser

        from ..cli import STATE_LABEL
        from ..matcher import UPLOADED

        r = core._root("%s - status" % config.APP_NAME, 780, 600)
        head = tk.Frame(r, bg=BG)
        head.pack(fill="x", padx=(16, _gutter()), pady=(14, 6))
        summary_lbl = _label(head, "", size=10, fg=MUTED, wraplength=720)
        summary_lbl.pack(anchor="w")

        t = _text_body(r, BG, margin_x=(16, 0), margin_y=(0, 14),
                       bg=PANEL, fg=INK, relief="flat", font=("Segoe UI", 10),
                       padx=14, pady=12, cursor="arrow", highlightthickness=0)

        t.tag_configure("h", foreground=MUTED, font=("Segoe UI", 8, "bold"),
                        spacing1=10, spacing3=6)
        t.tag_configure("ready", foreground=AMBER,
                        font=("Segoe UI", 10, "bold"))
        t.tag_configure("name", foreground=INK, font=("Segoe UI", 10))
        t.tag_configure("dim", foreground=MUTED, font=("Segoe UI", 9))

        links = [0]

        def add_link(url):
            links[0] += 1
            tag = "link-%d" % links[0]
            t.tag_configure(tag, foreground="#5cc4bb", underline=True)
            t.tag_bind(tag, "<Enter>", lambda e: t.configure(cursor="hand2"))
            t.tag_bind(tag, "<Leave>", lambda e: t.configure(cursor="arrow"))
            t.tag_bind(tag, "<Button-1>", lambda e, u=url: webbrowser.open(u))
            t.insert("end", url + "\n", tag)

        def snapshot():
            c = app.eng.counts()
            rows = app.store.recent(40)
            return c, rows, repr((sorted(c.items()), rows))

        def render(c, rows):
            summary_lbl.configure(text=", ".join(
                "%d %s" % (c[k], STATE_LABEL.get(k, k))
                for k in sorted(c) if c[k]) or "nothing yet")
            top = t.yview()[0]
            t.configure(state="normal")
            t.delete("1.0", "end")
            for tag in t.tag_names():
                if tag.startswith("link-"):
                    t.tag_delete(tag)

            done = [x for x in rows if x["state"] == UPLOADED and x["short_code"]]
            rest = [x for x in rows if x not in done]

            if done:
                t.insert("end", "READY FOR REVIEW\n", "h")
                for x in done:
                    t.insert("end", "Ready for review  ", "ready")
                    t.insert("end", "%s\n" % app.eng._label(x), "name")
                    t.insert("end", "    ")
                    add_link("%s/wowslegends/replays/?r=%s"
                             % (config.SITE_BASE, x["short_code"]))

            if rest:
                t.insert("end", "EVERYTHING ELSE\n", "h")
                for x in rest:
                    t.insert("end", "%-22s" % STATE_LABEL.get(x["state"], x["state"]),
                             "dim")
                    t.insert("end", "%s" % app.eng._label(x), "name")
                    extra = x["note"] or x["last_error"] or ""
                    t.insert("end", ("  %s" % extra if extra else "") + "\n", "dim")

            if not rows:
                t.insert("end", "Nothing seen yet. Play a battle and capture both "
                                "scorecards.\n", "dim")

            t.configure(state="disabled")
            t.yview_moveto(top)

        last = [None]

        def tick():
            c, rows, sig = snapshot()
            if sig != last[0]:
                last[0] = sig
                render(c, rows)

        try:
            tick()
        except Exception as e:   # a failed read must not kill the window
            app.log.error("status window refresh failed (%s)" % e)
        _poll(r, REFRESH_MS, tick, app.log, "status window")
        r.mainloop()

    core._thread(build, app.log, "the status window")


def open_doctor(app):
    _text_window("Debrief Uploader - diagnosis", app.eng.diagnose(), app.log,
                 "the diagnosis window")


def _version():
    from .. import __version__
    return __version__
