"""Tray windows: settings, status, diagnosis, sign-in.

Everything here is a thin shell over the same code the CLI drives, so the tray
is a second front end rather than a second implementation. Each window opens on
its own thread with its own Tk root -- pystray owns the main thread -- and any
failure is logged with a pointer at the equivalent command, because a GUI that
cannot open must not become a dead end.
"""
import os
import sys
import threading
import time

from . import autostart, config, oauth
from .api import ApiError

BG = "#0b1620"
PANEL = "#0e1d27"
LINE = "#1b2f3d"
INK = "#e4eef4"
MUTED = "#8aa2b1"
AMBER = "#ffd166"
AMBER_INK = "#1a1200"

VISIBILITY = [("Public - shared with the community", "public"),
              ("Fleet - only my fleet", "fleet"),
              ("Private - only me", "private"),
              ("Use my site default", "")]

# Training rooms get their own choice, and "same as above" is offered rather
# than assumed -- someone recording a practice session deliberately may well
# want it shared.
TRAINING_VISIBILITY = [("Private - only me", "private"),
                       ("Fleet - only my fleet", "fleet"),
                       ("Public - shared with the community", "public"),
                       ("Same as above", "")]


def _thread(fn, log, what):
    def go():
        try:
            fn()
        except Exception as e:
            log.error("could not open %s (%s) - the same thing is available "
                      "from Debrief.cmd" % (what, e))
        finally:
            # Free this window's Tk objects HERE, on the thread that made
            # them. Widgets, bindings, images and variables that reference
            # each other in cycles outlive the window and are otherwise freed
            # by the cyclic GC on whichever thread runs next - and freeing a
            # Tcl interpreter from the wrong thread aborts the whole app:
            # "Tcl_AsyncDelete: async handler deleted by the wrong thread"
            # (Stargatecraft, PR #6: Status closed, then Settings opened).
            # tests/test_ui_threads.py reproduces it.
            import gc
            gc.collect()
    threading.Thread(target=go, daemon=True).start()


# Pixels per 96-dpi pixel. The tray makes the process DPI-aware on Windows
# (PR #2), so Windows no longer bitmap-stretches these windows: fonts, given
# in points, come out at the real DPI, while sizes given in pixels would not.
# Every pixel size below goes through _px() so the layout grows with the
# text. One value per process: system-aware DPI does not change while
# running.
_SCALE = 1.0


def _poll(widget, ms, fn, log=None, what="window"):
    """Call fn() every ms milliseconds while widget lives; fn returning False
    stops it.

    One home for what every self-refreshing window needs. A call still
    pending when the window closes fires into the destroyed window (the
    Tcl_AsyncDelete crash family), so it is cancelled on <Destroy> -- with a
    bare "after cancel", because after_cancel() also unregisters the
    callback, which Tk 8.5 refuses mid-destroy. A failed refresh is logged,
    not raised: one bad read must not kill the window."""
    job = [None]

    def run():
        job[0] = None
        if not widget.winfo_exists():
            return
        again = True
        try:
            again = fn()
        except Exception as e:
            if log is not None:
                log.error("%s refresh failed (%s)" % (what, e))
        if again is not False and widget.winfo_exists():
            job[0] = widget.after(ms, run)

    def stop(e):
        # a binding on a toplevel also fires for each of its children
        if e.widget is not widget or not job[0]:
            return
        try:
            widget.tk.call("after", "cancel", job[0])
        except Exception:
            pass
        job[0] = None

    widget.bind("<Destroy>", stop, add="+")
    job[0] = widget.after(ms, run)
    return job


def _px(n):
    return int(round(n * _SCALE))

def _root(title, w, h):
    import tkinter as tk
    global _SCALE
    r = tk.Tk()
    # built hidden, then sized, placed and shown in one go: no flash of a
    # default-sized window jumping to the centre
    r.withdraw()
    # 96 px per inch is 100% on Windows; never shrink below it (macOS
    # reports 72).
    _SCALE = max(1.0, r.winfo_fpixels("1i") / 96.0)
    return _dress(r, title, w, h)


def _toplevel(parent, title, w, h):
    """A further window on the SAME thread and Tk as `parent` (Review's
    screenshot picker and full-size viewer), dressed like every other
    window. A second Tk on a second thread would be one more interpreter
    to free on the right thread; a Toplevel goes with its parent and its
    objects are collected by the parent thread's gc.collect()."""
    import tkinter as tk
    t = tk.Toplevel(parent)
    t.withdraw()
    return _dress(t, title, w, h)


def _set_title(r, title):
    """Retitle a window, our own title bar included."""
    r.title(title)
    lbl = getattr(r, "_title_label", None)
    if lbl is not None and lbl.winfo_exists():
        lbl.configure(text=title)


def _dress(r, title, w, h):
    r.title(title)
    r.configure(bg=BG)
    _set_size(r, min(_px(w), r.winfo_screenwidth() - 40),
              min(_px(h), r.winfo_screenheight() - 80))
    r.minsize(_px(360), _px(240))
    if os.name == "nt":
        from . import winframe
        try:
            winframe.frame(r, title, _px, BG, _res("app.ico"))
        except Exception:
            pass                # plain native title bar
        winframe.set_icon(r, _res("app.ico"))
    else:
        try:
            r.iconbitmap(_res("app.ico"))
        except Exception:
            pass

    def show():
        # the title bar first: once the native one is gone the client area
        # is the whole window, so the size set AFTER it is the outer size.
        # Placed first, the window came out a caption taller than asked and
        # its footer slid under the taskbar (CI screenshot, 2026-10-08).
        if os.name == "nt":
            try:
                winframe.finish(r, _px)
            except Exception:
                pass
        _place(r)
        r.deiconify()

    r.after(30, show)
    return r


def _res(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "resources", *parts)


def _label(parent, text, size=10, fg=INK, bold=False, bg=BG, **kw):
    """A text label. With wraplength, the text wraps to the parent's width
    and keeps following it as the window is resized, wider as well as
    narrower; wraplength is only the width used before the first layout.
    A fixed wrap made the text wider than its panel once the window could be
    resized narrower, and Tk centres an over-wide label, so both edges were
    cut off; a capped one left it narrow in a wide window (Stargatecraft,
    PR #6).

    Only give wraplength to a label whose parent is as wide as the window
    lets it be (packed with fill="x", or the body itself). In a parent that
    sizes itself to its contents the label and parent would shrink each
    other down to the minimum."""
    import tkinter as tk
    wrap = None
    if "wraplength" in kw:
        wrap = kw["wraplength"] = _px(kw["wraplength"])
    kw.setdefault("justify", "left")
    lbl = tk.Label(parent, text=text, bg=bg, fg=fg,
                   font=("Segoe UI", size, "bold" if bold else "normal"), **kw)
    if wrap:
        def fit(e, lbl=lbl):
            if e.widget is parent and lbl.winfo_exists():
                lbl.configure(wraplength=max(_px(120), e.width - _px(28)))
        parent.bind("<Configure>", fit, add="+")
    return lbl


def _button(parent, text, cmd, primary=False):
    import tkinter as tk
    return tk.Button(parent, text=text, command=cmd, relief="flat", padx=12,
                     pady=3, cursor="hand2",
                     bg=AMBER if primary else "#16283a",
                     fg=AMBER_INK if primary else INK,
                     activebackground=AMBER if primary else "#1d3448",
                     font=("Segoe UI", 9, "bold" if primary else "normal"))


def _section(parent, title):
    import tkinter as tk
    box = tk.Frame(parent, bg=PANEL, highlightbackground=LINE,
                   highlightthickness=1)
    box.pack(fill="x", pady=(0, 10))
    _label(box, title.upper(), size=8, fg=MUTED, bold=True, bg=PANEL).pack(
        anchor="w", padx=12, pady=(9, 4))
    return box


class _ThinScroll:
    """A slim scrollbar in the app's colours, shown only while there is
    something to scroll.

    Why not none: the Settings scrollbar exists because a tester at 150%
    scaling never found the sections below the fold -- the window ended at
    the review checkbox, and Save and Startup were simply never seen. With
    the bar hidden the only cue left is a section peeking out at the bottom,
    and only if the window happens to cut one off. Why not tk.Scrollbar:
    Windows draws it natively and ignores its colours, so it was the one
    light-grey thing in a dark window (PR #6 removed it for that reason).
    A ttk style could recolour it, but a theme change is per Tk root and
    would also restyle Settings' OptionMenus. So it is drawn here: a
    rounded thumb on a canvas, that drags, pages on a click outside the
    thumb, and hides when everything fits.

    Visibility follows Firefox's overlay bars (Stargatecraft's suggestion,
    PR #6): hidden while the mouse is still, a slim low-contrast bar while
    it moves over the window, bright under the pointer or while dragging.
    It also shows for a moment when the window opens, which keeps the
    "there is more below" cue the tester needed.

    Use as the target's yscrollcommand: target.configure(yscrollcommand=sb.set)."""

    THUMB, HOVER = "#2a4152", "#c9d6df"
    LINGER_MS = 1200          # how long the bar stays after the mouse stops

    def __init__(self, parent, target, bg=BG):
        import tkinter as tk
        self.target = target
        self.first, self.last = 0.0, 1.0
        self.width = _px(8)
        self.c = tk.Canvas(parent, width=self.width, bg=bg, bd=0,
                           highlightthickness=0)
        self._grab = None        # pointer offset into the thumb while dragging
        self._hover = False
        self._awake = True       # shown on open, then only while the mouse moves
        self._sleep_job = None
        top = parent.winfo_toplevel()
        for ev in ("<Motion>", "<MouseWheel>"):
            top.bind(ev, lambda e: self._wake(), add="+")
        self._wake()
        self.c.bind("<Configure>", lambda e: self._draw())
        self.c.bind("<Button-1>", self._press)
        self.c.bind("<B1-Motion>", self._drag)
        self.c.bind("<ButtonRelease-1>", self._release)
        self.c.bind("<Enter>", lambda e: self._set_hover(True))
        self.c.bind("<Leave>", lambda e: self._set_hover(False))
        self.c.bind("<Destroy>", lambda e: self._cancel())

    def _cancel(self):
        # a fade timer left pending fires into a destroyed window
        if self._sleep_job:
            try:
                self.c.after_cancel(self._sleep_job)
            except Exception:
                pass
            self._sleep_job = None

    def pack(self, **kw):
        self.c.pack(**kw)

    def dock(self):
        """Into the right-hand gutter of its parent, over the space _gutter()
        keeps clear. Placed, not packed: the content never changes width when
        the bar appears, so nothing jumps or rewraps (Stargatecraft, PR #6:
        the buttons moved before the bar showed up)."""
        self.c.place(relx=1, x=-(self.width + _px(6)), y=0, relheight=1,
                     width=self.width)

    def set(self, first, last):
        self.first, self.last = float(first), float(last)
        self._draw()

    def _thumb(self):
        h = max(1, self.c.winfo_height())
        y0, y1 = self.first * h, self.last * h
        least = _px(28)          # never a sliver too small to grab
        if y1 - y0 < least:
            mid = (y0 + y1) / 2
            y0 = min(max(0, mid - least / 2), h - least)
            y1 = y0 + least
        return y0, y1, h

    def _wake(self):
        if not self.c.winfo_exists():
            return
        self._awake = True
        if self._sleep_job:
            self.c.after_cancel(self._sleep_job)
        self._sleep_job = self.c.after(self.LINGER_MS, self._sleep)
        self._draw()

    def _sleep(self):
        self._sleep_job = None
        if self._hover or self._grab is not None:
            self._sleep_job = self.c.after(self.LINGER_MS, self._sleep)
            return
        self._awake = False
        self._draw()

    def _draw(self):
        if not self.c.winfo_exists():
            return
        self.c.delete("all")
        if self.last - self.first >= 0.999:
            return               # everything fits: no bar at all
        if not (self._awake or self._hover or self._grab is not None):
            return               # the mouse is still: out of the way
        y0, y1, _ = self._thumb()
        w, pad = self.width, _px(2)
        # a round-capped line is a pill; the caps add half the width each end
        self.c.create_line(w / 2, y0 + w / 2 + pad, w / 2, y1 - w / 2 - pad,
                           width=w - pad, capstyle="round",
                           fill=self.HOVER if (self._hover or self._grab is not None)
                           else self.THUMB)

    def _set_hover(self, on):
        self._hover = on
        self._draw()

    def _press(self, e):
        y0, y1, h = self._thumb()
        if not (y0 <= e.y <= y1):    # outside the thumb: centre it there
            span = self.last - self.first
            self.target.yview_moveto(max(0.0, e.y / h - span / 2))
            y0, y1, h = self._thumb()
        self._grab = e.y - y0

    def _drag(self, e):
        if self._grab is None:
            return
        h = max(1, self.c.winfo_height())
        self.target.yview_moveto(max(0.0, (e.y - self._grab) / h))

    def _release(self, e):
        self._grab = None
        self._draw()


def _gutter():
    """Room kept clear on the right of every window's content for the slim
    scrollbar: the bar (8) and, outside it, more than the 5 px resize grip
    on the window's right edge, so the grip never sits on the bar."""
    return _px(8) + _px(6) + _px(2)


class _Body:
    """The scrolling content area every tray window puts its widgets in.

    One implementation, so each window reflows the same way (Stargatecraft,
    PR #6: "the widgets need to wrap the contents and adjust their width and
    height as necessary on all windows, not just in the settings"):

    - .inner is always exactly as wide as the window allows, so labels with
      a wraplength rewrap on every resize (see _label);
    - its height is whatever the content asks for: when that is more than
      the window, the slim fading bar appears and the wheel scrolls, instead
      of pack squeezing the last widgets (Review cut its own buttons in half);
    - the bar lives in a gutter that is always reserved, so the content
      keeps its width when the bar shows up;
    - center=True centres short content vertically while it fits.

    Text windows (status, diagnosis) scroll their Text instead; _text_body()
    gives them the same bar in the same gutter."""

    def __init__(self, r, bg=BG, padx=16, pady=14, center=False):
        import tkinter as tk
        self.padx, self.pady, self.center = padx, pady, center
        self.host = tk.Frame(r, bg=bg)
        self.host.pack(side="top", fill="both", expand=True)
        self.canvas = tk.Canvas(self.host, bg=bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.bar = _ThinScroll(self.host, self.canvas, bg=bg)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.bar.dock()
        self.host._scroll = self.bar        # tests find the body by this
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window(padx, pady, window=self.inner,
                                              anchor="nw")
        self.inner.bind("<Configure>", self._layout, add="+")
        self.canvas.bind("<Configure>", self._layout, add="+")
        r.bind_all("<MouseWheel>", self._wheel, add="+")
        r.bind_all("<Button-4>", self._wheel, add="+")     # X11 wheel
        r.bind_all("<Button-5>", self._wheel, add="+")

    def _layout(self, e=None):
        c = self.canvas
        if not c.winfo_exists():
            return
        w, h = c.winfo_width(), c.winfo_height()
        if w <= 1:
            return                   # not laid out yet
        right = max(self.padx, _gutter())
        c.itemconfigure(self._win, width=max(_px(120), w - self.padx - right))
        ih = self.inner.winfo_reqheight()
        y = self.pady
        if self.center:
            y = max(self.pady, (h - ih) // 2)
        c.coords(self._win, self.padx, y)
        c.configure(scrollregion=(0, 0, w, y + ih + self.pady))

    def fits(self):
        return self.bar.last - self.bar.first >= 0.999

    def _wheel(self, e):
        if self.fits() or not self.canvas.winfo_exists():
            return
        if getattr(e, "num", None) in (4, 5):
            step = -1 if e.num == 4 else 1
        else:
            step = int(-e.delta / 120) or (-1 if e.delta > 0 else 1)
        self.canvas.yview_scroll(step, "units")


def _text_body(r, around, margin_x=0, margin_y=0, **text_kw):
    """A scrolling Text with the same slim bar, in the same gutter, as
    _Body. wrap="word" unless asked otherwise, so lines follow the window's
    width instead of running off its right edge. margin_x/margin_y go around
    it (pack padding) in the colour `around`; padx/pady/bg are the Text's
    own."""
    import tkinter as tk
    host = tk.Frame(r, bg=around)
    host.pack(side="top", fill="both", expand=True, padx=margin_x,
              pady=margin_y)
    text_kw.setdefault("wrap", "word")
    t = tk.Text(host, **text_kw)
    t.pack(side="left", fill="both", expand=True, padx=(0, _gutter()))
    sb = _ThinScroll(host, t, bg=around)
    t.configure(yscrollcommand=sb.set)
    sb.dock()
    host._scroll = sb                       # tests find the body by this
    return t


def _text_window(title, lines, log, what):
    def build():
        r = _root(title, 760, 560)
        t = _text_body(r, PANEL, bg=PANEL, fg=INK, insertbackground=INK,
                       relief="flat", font=("Consolas", 9), padx=12, pady=10,
                       highlightthickness=0)
        t.insert("1.0", "\n".join(lines))
        t.configure(state="disabled")
        r.mainloop()
    _thread(build, log, what)


def _set_size(r, w, h):
    """Size in pixels, applied with the position when the window is shown
    (_root). Windows call this instead of r.geometry()."""
    r._wh = (int(w), int(h))


def _work_area(r):
    """Left, top, right, bottom of the screen minus the taskbar."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            rc = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rc), 0):
                return rc.left, rc.top, rc.right, rc.bottom   # SPI_GETWORKAREA
        except Exception:
            pass
    return 0, 0, r.winfo_screenwidth(), r.winfo_screenheight()


def _place(r):
    """Centre in the work area at the size _set_size() asked for, never
    larger than it: a window taller than the space above the taskbar hides
    its own footer (Done, the save note)."""
    left, top, right, bottom = _work_area(r)
    w, h = r._wh
    w, h = min(w, right - left), min(h, bottom - top)
    x = left + (right - left - w) // 2
    y = top + (bottom - top - h) // 2
    r.geometry("%dx%d+%d+%d" % (w, h, x, y))


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

        from .cli import STATE_LABEL
        from .matcher import UPLOADED

        r = _root("%s - status" % config.APP_NAME, 780, 600)
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

    _thread(build, app.log, "the status window")


def open_doctor(app):
    _text_window("Debrief Uploader - diagnosis", app.eng.diagnose(), app.log,
                 "the diagnosis window")


def _version():
    from . import __version__
    return __version__


# --------------------------------------------------------------- sign in ----

def open_sign_in(app, on_done=None):
    def build():
        import tkinter as tk
        r = _root("Sign in to GamingDiver", 420, 380)
        pad = _Body(r, padx=18, pady=16).inner
        _label(pad, "Use the same account as gamingdiver.com.", fg=MUTED,
               size=9, wraplength=380).pack(anchor="w", pady=(0, 10))

        # Most accounts were made with Google or Discord and have no password
        # (Greg 2026-10-05: "I used Google as my signin so I do not have an
        # email/password"), so the browser sign-in comes first.
        prov = tk.Frame(pad, bg=BG)
        prov.pack(fill="x", pady=(0, 10))
        prov_buttons = []
        result = {}

        def finish_ok():
            r.lift()
            app.log.info("signed in as %s" % (app.client.session.email or ""))
            app.eng.unblock()
            if on_done:
                on_done()
            r.destroy()

        def browser(provider):
            for b_ in prov_buttons:
                b_.configure(state="disabled")
            msg.configure(text="Your browser opened: sign in with %s there, "
                               "then come back here." % provider.title(), fg=MUTED)
            result.clear()

            def work():
                try:
                    oauth.sign_in_browser(app.client, provider)
                    result["ok"] = True
                except ApiError as e:
                    result["err"] = e.message
                except Exception as e:   # a dead window must not be the result
                    result["err"] = "sign-in failed (%s)" % e

            threading.Thread(target=work, daemon=True).start()

            def poll():
                if not result:
                    r.after(300, poll)
                    return
                if result.get("ok"):
                    finish_ok()
                    return
                for b_ in prov_buttons:
                    b_.configure(state="normal")
                app.log.error("browser sign-in failed: %s" % result["err"])
                msg.configure(text=result["err"], fg="#f0a173")
                r.lift()                 # it is behind the browser by now
            r.after(300, poll)

        for provider in oauth.PROVIDERS:
            bt = _button(prov, "Sign in with %s" % provider.title(),
                         lambda p_=provider: browser(p_), primary=True)
            bt.pack(fill="x", pady=(0, 6))
            prov_buttons.append(bt)

        _label(pad, "Or with email and password:", fg=MUTED, size=9,
               wraplength=380).pack(
            anchor="w", pady=(4, 6))

        _label(pad, "Email", size=9).pack(anchor="w")
        email = tk.Entry(pad, bg=PANEL, fg=INK, insertbackground=INK,
                         relief="flat", font=("Segoe UI", 10))
        email.pack(fill="x", ipady=4, pady=(2, 10))
        if app.client.session.email:
            email.insert(0, app.client.session.email)

        _label(pad, "Password", size=9).pack(anchor="w")
        pw = tk.Entry(pad, show="•", bg=PANEL, fg=INK,
                      insertbackground=INK, relief="flat",
                      font=("Segoe UI", 10))
        pw.pack(fill="x", ipady=4, pady=(2, 10))

        msg = _label(pad, "", size=9, fg=MUTED, wraplength=380)
        msg.pack(anchor="w", pady=(0, 8))

        def submit(*_):
            msg.configure(text="Signing in...", fg=MUTED)
            r.update_idletasks()
            try:
                app.client.sign_in_password(email.get().strip(), pw.get())
            except ApiError as e:
                msg.configure(text=e.message, fg="#f0a173")
                return
            finish_ok()

        pw.bind("<Return>", submit)
        row = tk.Frame(pad, bg=BG)
        row.pack(fill="x")
        _button(row, "Sign in", submit).pack(side="left")
        _button(row, "Cancel", r.destroy).pack(side="left", padx=6)
        email.focus_set()
        r.mainloop()
    _thread(build, app.log, "the sign-in window")


# ---------------------------------------------------------------- review ----

def _clock(t):
    """Local wall-clock time of an epoch; the date too when not today."""
    import time
    lt = time.localtime(t)
    if time.strftime("%Y%m%d", lt) == time.strftime("%Y%m%d"):
        return time.strftime("%H:%M:%S", lt)
    return time.strftime("%d %b %H:%M:%S", lt)


def _span(seconds):
    s = int(round(abs(seconds)))
    if s < 60:
        return "%d s" % s
    m, s = divmod(s, 60)
    if m < 60:
        return "%d min %d s" % (m, s) if s else "%d min" % m
    h, m = divmod(m, 60)
    return "%d h %d min" % (h, m)


def _vs_battle(t, t_close):
    """A screenshot's time against its battle's, the clock the matcher
    itself pairs on: the replay file's save time (matcher.py)."""
    if not t_close:
        return ""
    d = t - t_close
    if d >= 0:
        return "%s after the battle" % _span(d)
    return "%s before the battle's replay was saved" % _span(d)


def _load_preview(path, longest):
    """A screenshot decoded once and scaled to `longest` px on its longest
    side. A JPEG decodes straight at reduced size (draft)."""
    from PIL import Image
    with Image.open(path) as im:
        try:
            im.draft("RGB", (longest, longest))
        except Exception:
            pass
        im = im.convert("RGB")
    im.thumbnail((longest, longest), Image.LANCZOS)
    return im


class _ScaledThumbs:
    """Screenshot previews that grow and shrink with the window.

    Greg (PR #6): "the images dont scale and you cant extend them, making
    identification hard" - they were fixed 150x84 thumbnails. Each preview
    now takes an equal share of its panel's width, between MIN and MAX
    (96-dpi) px, keeping its aspect; when the panel is too narrow for them
    side by side they wrap onto more rows. Each file is decoded once into
    `cache` (a 4K PNG takes a noticeable moment) and only rescaled, once the
    resize has settled. Click one to see it full size (_view_image).

    items: [(path, caption)]. Pack .frame with fill="x"."""

    MIN, MAX, GAP = 150, 640, 8
    SOURCE = 1280               # decoded once at this size, scaled from it
    SETTLE_MS = 60

    def __init__(self, parent, items, cache, bg=PANEL):
        import tkinter as tk
        self.frame = tk.Frame(parent, bg=bg)
        self.frame._thumbs = self              # tests find it by this
        self.cells = []
        self.per = self.cols = None
        self._job = None
        for path, caption in items:
            src = cache.get(path)
            if src is None:
                try:
                    src = cache[path] = _load_preview(path, self.SOURCE)
                except Exception:
                    src = None
            cell = tk.Frame(self.frame, bg=bg)
            img = tk.Label(cell, bg=bg, bd=0)
            if src is None:
                img.configure(text="(cannot open %s)" % os.path.basename(path),
                              fg=MUTED, font=("Segoe UI", 8))
            else:
                img.configure(cursor="hand2")
                img.bind("<Button-1>",
                         lambda e, p=path: _view_image(e.widget.winfo_toplevel(), p))
            img.pack(anchor="w")
            cap = tk.Label(cell, text=caption, bg=bg, fg=MUTED, anchor="w",
                           justify="left", font=("Segoe UI", 8))
            cap.pack(anchor="w", fill="x", pady=(2, 0))
            self.cells.append({"cell": cell, "img": img, "cap": cap,
                               "src": src, "photo": None})
        self.frame.bind("<Configure>", self._resized, add="+")
        self.frame.bind("<Destroy>", self._cancel, add="+")

    def _cancel(self, e=None):
        # a bare "after cancel": after_cancel() also unregisters the
        # callback, which Tk refuses while the window is being destroyed
        if self._job:
            try:
                self.frame.tk.call("after", "cancel", self._job)
            except Exception:
                pass
            self._job = None

    def _resized(self, e):
        if e.widget is not self.frame:
            return
        self._cancel()
        self._job = self.frame.after(self.SETTLE_MS, lambda w=e.width: self.fit(w))

    def fit(self, width):
        """Lay the previews out for a panel `width` px wide."""
        from PIL import Image, ImageTk
        self._job = None
        if not self.cells or width <= 1 or not self.frame.winfo_exists():
            return
        gap, lo, hi = _px(self.GAP), _px(self.MIN), _px(self.MAX)
        n = len(self.cells)
        cols = max(1, min(n, (width + gap) // (lo + gap)))
        per = max(lo, min(hi, (width - gap * (cols - 1)) // cols))
        if cols != self.cols:
            for i, c in enumerate(self.cells):
                c["cell"].grid(row=i // cols, column=i % cols, sticky="nw",
                               padx=(0, gap if i % cols < cols - 1 else 0),
                               pady=(0, gap))
            self.cols = cols
        if self.per is not None and abs(per - self.per) < 4:
            return                   # not worth a re-scale
        self.per = per
        for c in self.cells:
            c["cap"].configure(wraplength=per)
            src = c["src"]
            if src is None:
                continue
            sw, sh = src.size
            # a tall capture is capped at a 4:3 box, never a towering one
            k = min(per / float(sw), per * 0.75 / sh)
            size = (max(1, int(sw * k)), max(1, int(sh * k)))
            c["photo"] = ImageTk.PhotoImage(src.resize(size, Image.LANCZOS),
                                            master=self.frame)
            c["img"].configure(image=c["photo"])

    def widths(self):
        return [c["photo"].width() if c["photo"] else 0 for c in self.cells]


def _view_image(parent, path):
    """One screenshot as large as the screen allows, in its own window on
    the parent's thread. Click it or press Escape to close; resizing the
    window rescales it. Returns the window, or None if the file cannot be
    read."""
    import tkinter as tk
    from PIL import Image, ImageTk
    try:
        with Image.open(path) as im:
            src = im.convert("RGB")
    except Exception:
        return None
    iw, ih = src.size
    left, top, right, bottom = _work_area(parent)
    bar = _px(32) if os.name == "nt" else 0
    k = min(1.0, (right - left - _px(60)) / float(iw),
            (bottom - top - _px(60) - bar) / float(ih))
    v = _toplevel(parent, "%s - click or Esc to close" % os.path.basename(path),
                  360, 240)
    _set_size(v, max(_px(360), int(iw * k)), max(_px(240), int(ih * k) + bar))
    c = tk.Canvas(v, bg="#000000", highlightthickness=0, bd=0, cursor="hand2")
    c.pack(fill="both", expand=True)
    state = {"photo": None, "size": None, "job": None}

    def draw():
        state["job"] = None
        if not c.winfo_exists():
            return
        w, h = c.winfo_width(), c.winfo_height()
        if w <= 1 or h <= 1:
            return
        s = min(w / float(iw), h / float(ih))
        size = (max(1, int(iw * s)), max(1, int(ih * s)))
        if size != state["size"]:
            state["size"] = size
            state["photo"] = ImageTk.PhotoImage(
                src.resize(size, Image.LANCZOS), master=v)
        c.delete("all")
        c.create_image(w // 2, h // 2, image=state["photo"], anchor="center")

    def later(e=None):
        if state["job"]:
            c.after_cancel(state["job"])
        state["job"] = c.after(60, draw)

    def close(e=None):
        if v.winfo_exists():
            v.destroy()

    c.bind("<Configure>", later)
    c.bind("<Button-1>", close)
    v.bind("<Escape>", close)
    v.protocol("WM_DELETE_WINDOW", close)
    v.after(60, v.focus_force)
    v._canvas, v._state = c, state            # tests look inside
    return v


def _shot_candidates(app, row, attached, now=None, limit=40):
    """The screenshots someone might mean for this battle, newest first:
    every one in the watched folders from 30 minutes before its replay was
    saved to 6 hours after, plus whatever is attached now. The scan's own
    rules apply (excluded folders, files from before watching started,
    the size floor). With nothing near the battle, the newest dozen - so a
    battle reviewed days later still offers something. Returns
    [(path, t)]."""
    from . import watcher
    s = app.s
    t_close = row["t_close"] or 0
    found = watcher.scan_shots(s.get("shot_dirs") or [],
                               (s.get("shot_min_kb") or 0) * 1024, None, now,
                               s.get("exclude_dirs"), s.get("watching_since"))
    near = [(p, t) for p, t, _ in found
            if t_close and t_close - 1800 <= t <= t_close + 6 * 3600]
    if not near:
        near = [(p, t) for p, t, _ in found[:12]]
    near = near[:limit]
    have = {p for p, _ in near}
    for p in attached:
        if p in have:
            continue
        sh = app.store.shot(p)
        if sh is not None:
            near.append((p, sh["t"] or 0))
    near.sort(key=lambda x: x[1], reverse=True)
    return near


def _pick_shots(parent, app, row, on_done):
    """Choose which screenshots go up with this battle, or none.

    Opens on the review window's thread. Lists the screenshot folders'
    files around the battle's time, newest first, each with its preview,
    its time against the battle's, and who has it now; the ones attached
    now are ticked. "Use these" hands the pick to Engine.set_shots(), which
    is what the upload then sends."""
    import queue
    import tkinter as tk
    from PIL import ImageTk

    from . import matcher

    old = getattr(parent, "_picker", None)
    if old is not None and old.winfo_exists():
        old.deiconify()
        old.lift()
        return old

    md5 = row["md5"]
    t_close = row["t_close"] or 0
    attached = [sh["path"] for sh in app.store.shots_for(md5)]
    cands = _shot_candidates(app, row, attached)
    limit = app.s["max_shots"]

    w = _toplevel(parent, "%s - choose screenshots" % config.APP_NAME, 700, 660)
    parent._picker = w
    foot = tk.Frame(w, bg=BG)
    foot.pack(side="bottom", fill="x", padx=(16, _gutter()), pady=(4, 12))
    head = tk.Frame(w, bg=BG)
    head.pack(side="top", fill="x", padx=(16, _gutter()), pady=(12, 0))
    _label(head, app.eng._label(row), size=11, bold=True, wraplength=640,
           anchor="w").pack(fill="x")
    _label(head, "Its replay was saved at %s. Tick this battle's results "
                 "screenshots (the personal scorecard and the team result), "
                 "up to %d, or none. Click a picture to see it full size."
           % (_clock(t_close) if t_close else "an unknown time", limit),
           size=9, fg=MUTED, wraplength=640, anchor="w").pack(fill="x",
                                                              pady=(2, 0))
    pad = _Body(w, padx=16, pady=10).inner

    blank = tk.PhotoImage(master=w, width=_px(200), height=_px(112))
    tiles = []                     # (path, t, var, tile)
    live = {}                      # path -> (checkbox, owner label, free)
    pics = []

    def owner_of(path):
        """'' if free or ours; else who has it, and whether it can move."""
        sh = app.store.shot(path)
        if sh is None or not sh["replay_md5"] or sh["replay_md5"] == md5:
            return "", True
        o = app.store.replay(sh["replay_md5"])
        name = app.eng._label(o) if o is not None else "another battle"
        if sh["uploaded"] or (o is not None and o["state"] not in
                              matcher.REVIEWABLE):
            return "already sent with %s" % name, False
        return "now matched to %s - ticking moves it here" % name, True

    msg = _label(foot, "", size=9, fg=MUTED)

    def count():
        return sum(1 for _, _, v, _ in tiles if v.get())

    def toggled(var, tile):
        if var.get() and count() > limit:
            var.set(False)
            msg.configure(text="At most %d - untick one first." % limit,
                          fg="#f0a173")
        else:
            msg.configure(text="")
        tile.configure(highlightbackground=AMBER if var.get() else LINE)
        n = count()
        use.configure(text="Use these %d" % n if n else "Use none")

    for path, t in cands:
        tile = tk.Frame(pad, bg=PANEL, highlightthickness=1,
                        highlightbackground=LINE)
        tile.pack(fill="x", pady=4)
        pic = tk.Label(tile, image=blank, bg=PANEL, bd=0, cursor="hand2")
        pic.pack(side="left", anchor="n", padx=8, pady=8)
        pic.bind("<Button-1>", lambda e, p=path: _view_image(w, p))
        pics.append(pic)
        info = tk.Frame(tile, bg=PANEL)
        info.pack(side="left", fill="both", expand=True, pady=8, padx=(0, 8))
        _label(info, _clock(t), size=10, bold=True, bg=PANEL, anchor="w").pack(
            fill="x")
        _label(info, _vs_battle(t, t_close), size=9, bg=PANEL, anchor="w",
               wraplength=360).pack(fill="x")
        _label(info, os.path.basename(path), size=8, fg=MUTED, bg=PANEL,
               anchor="w", wraplength=360).pack(fill="x")
        whose, free = owner_of(path)
        if path in attached:
            whose = "attached to this battle now"
        whose_lbl = _label(info, whose, size=8, fg=AMBER if free else MUTED,
                           bg=PANEL, anchor="w", wraplength=360)
        if whose:
            whose_lbl.pack(fill="x")
        var = tk.BooleanVar(w, value=path in attached)
        cb = tk.Checkbutton(info, text="Use this one", variable=var, bg=PANEL,
                            fg=INK, selectcolor=BG, activebackground=PANEL,
                            activeforeground=INK, font=("Segoe UI", 9))
        cb.configure(command=lambda v=var, tl=tile: toggled(v, tl))
        if not free:
            cb.configure(state="disabled")
        cb.pack(anchor="w", pady=(4, 0))
        if var.get():
            tile.configure(highlightbackground=AMBER)
        tiles.append((path, t, var, tile))
        live[path] = (cb, whose_lbl, free)

    if not cands:
        _label(pad, "No screenshots in your screenshot folders (Settings). "
                    "You can still upload the battle without any.",
               size=9, fg=MUTED, wraplength=640).pack(fill="x", pady=8)

    # Previews decode off the Tk thread: a few dozen full-screen PNGs take
    # seconds. The worker touches no Tk object; the window takes each image
    # from the queue on its own thread.
    q, stop = queue.Queue(), threading.Event()
    paths = [p for p, _ in cands]
    size = _px(200)

    def work():
        for i, p in enumerate(paths):
            if stop.is_set():
                return
            try:
                q.put((i, _load_preview(p, size)))
            except Exception:
                pass
    threading.Thread(target=work, daemon=True).start()
    photos = {}

    def drain():
        if not w.winfo_exists():
            return
        try:
            while True:
                i, im = q.get_nowait()
                photos[i] = ImageTk.PhotoImage(im, master=w)
                pics[i].configure(image=photos[i])
        except queue.Empty:
            pass
        if len(photos) < len(paths):
            w.after(60, drain)
    w.after(60, drain)

    def close():
        stop.set()
        if w.winfo_exists():
            w.destroy()

    def apply():
        chosen = [p for p, _, v, _ in sorted(tiles, key=lambda x: x[1]) if v.get()]
        try:
            app.eng.set_shots(md5, chosen)
        except ValueError as e:
            msg.configure(text=str(e), fg="#f0a173")
            return
        except Exception as e:
            app.log.error("could not change the screenshots (%s)" % e)
            msg.configure(text="Could not change them (%s)." % e, fg="#f0a173")
            return
        close()
        on_done()

    def watch():
        """Keep the picker true while it is open (Stargatecraft, PR #6: a
        review finished elsewhere left its screenshots tickable here, and
        Use these then failed). The battle resolved -> nothing to choose
        for, so it closes. A screenshot sent with another battle meanwhile
        -> unticked and greyed, as if it had been when the picker opened."""
        if not w.winfo_exists():
            return False
        try:
            now_row = app.store.replay(md5)
            if now_row is None or now_row["state"] not in matcher.REVIEWABLE:
                close()
                return False
            for path, _, var, tile in tiles:
                cb, lbl, was_free = live[path]
                whose, free = owner_of(path)
                if free or not was_free:
                    continue
                live[path] = (cb, lbl, False)
                var.set(False)
                cb.configure(state="disabled")
                tile.configure(highlightbackground=LINE)
                lbl.configure(text=whose, fg=MUTED)
                if not lbl.winfo_manager():
                    lbl.pack(fill="x")
                n = count()
                use.configure(text="Use these %d" % n if n else "Use none")
        except Exception as e:     # a failed read must not kill the picker
            app.log.error("screenshot picker refresh failed (%s)" % e)

    w.protocol("WM_DELETE_WINDOW", close)
    w.bind("<Escape>", lambda e: close())
    msg.pack(side="left")
    _button(foot, "Cancel", close).pack(side="right", padx=(6, 0))
    n0 = sum(1 for _, _, v, _ in tiles if v.get())
    use = _button(foot, "Use these %d" % n0 if n0 else "Use none", apply,
                  primary=True)
    use.pack(side="right")
    _poll(w, 1000, watch, app.log, "screenshot picker")
    w._tiles, w._apply, w._watch = tiles, apply, watch   # tests drive these
    return w


# One Review window at a time. pystray calls on_review from its own thread
# for every click - a double-click on the tray icon is two - and each used
# to build an independent window from a one-off read of the store.
_REVIEW = {"open": False, "front": False, "on_change": None}
_REVIEW_LOCK = threading.Lock()


def _review_title(held):
    if held:
        return "%s - %d to review" % (config.APP_NAME, len(held))
    return "%s - nothing to review" % config.APP_NAME


def open_review(app, on_change=None):
    """Resolve the battles the matcher would not guess at.

    The title is derived from the CONTENT, not assumed. It used to read
    "needs you" over a window saying "Nothing needs you." -- which is the sort
    of small dishonesty that makes someone stop trusting the rest of it.

    It stays current (Greg, PR #6: "The Review window does not update"). It
    used to read the store once, when it opened, and Upload/Skip only
    deleted that battle's panel: the title kept saying "1 to review" over an
    empty window, and a battle held after it opened never appeared. Now it
    re-reads every REFRESH_MS and redraws whenever the held battles, their
    screenshots or notes change, title included; it says so when there is
    nothing to review; and opening it again brings this window forward
    instead of building a second one.
    """
    with _REVIEW_LOCK:
        if on_change is not None:
            _REVIEW["on_change"] = on_change
        if _REVIEW["open"]:
            _REVIEW["front"] = True
            return
        _REVIEW["open"], _REVIEW["front"] = True, False

    def build():
        try:
            _review_window(app)
        finally:
            with _REVIEW_LOCK:
                _REVIEW["open"] = False

    try:
        _thread(build, app.log, "the review window")
    except Exception:
        with _REVIEW_LOCK:
            _REVIEW["open"] = False
        raise


def _review_window(app, refresh_ms=1000):
    import tkinter as tk
    from tkinter import ttk

    from . import matcher
    from .engine import Engine

    def settling():
        """(count, decided-at) of screenshots taken but not matched yet: the
        30-90 s the matcher waits in case another one is coming. Shown, so a
        battle with fresh screenshots is not a mystery for that minute
        (Stargatecraft, PR #6: "it did appear ... but it took a minute-ish")."""
        return app.eng.settling()

    def snapshot():
        held = list(app.store.replays([matcher.HELD]))
        waiting = list(app.store.replays([matcher.AWAITING_SHOTS]))
        shots = {x["md5"]: list(app.store.shots_for(x["md5"])) for x in held}
        settle = settling()
        sig = repr(([(x["md5"], x["note"], [s["path"] for s in shots[x["md5"]]])
                     for x in held],
                    [(x["md5"], app.eng._label(x), x["note"]) for x in waiting],
                    settle))
        return held, waiting, shots, settle, sig

    first = snapshot()
    r = _root(_review_title(first[0]), 680,
              620 if first[0] or first[1] else 300)
    # six held battles are taller than the window: they scroll, rather
    # than pack squashing the last one's buttons (Stargatecraft, PR #6)
    body = _Body(r, padx=16, pady=16)
    cache = {}                  # path -> decoded preview, kept across redraws
    page = [None]
    last = [None]

    def changed():
        cb = _REVIEW.get("on_change")
        if cb:
            try:
                cb()
            except Exception as e:
                app.log.error("could not update the tray (%s)" % e)

    def act(fn, md5):
        def go():
            try:
                fn(md5)
            except Exception as e:
                app.log.error("review: %s" % e)
            refresh(force=True)
            changed()
        return go

    def card(pg, row, shots, waiting):
        md5 = row["md5"]
        t_close = row["t_close"] or 0
        box = tk.Frame(pg, bg=PANEL, highlightbackground=LINE,
                       highlightthickness=1)
        box.pack(fill="x", pady=6)
        # a replay file name has no spaces; Tk still breaks it at the
        # panel's edge rather than cutting it off
        _label(box, app.eng._label(row), size=11, bold=True,
               bg=PANEL, anchor="w", wraplength=580).pack(
            fill="x", padx=10, pady=(8, 0))
        _label(box, row["note"] or "needs confirmation", size=9, fg=MUTED,
               bg=PANEL, anchor="w", wraplength=580).pack(fill="x", padx=10)
        if t_close:
            _label(box, "Replay saved at %s" % _clock(t_close), size=8,
                   fg=MUTED, bg=PANEL, anchor="w", wraplength=580).pack(
                fill="x", padx=10)

        # which screenshots go up with it, and why these (Greg, PR #6: "i
        # dont trust the auto matching")
        sec = tk.Frame(box, bg=PANEL)
        sec.pack(fill="x", padx=10, pady=(8, 0))
        moved = row["note"] == Engine.MOVED_NOTE
        chosen = moved or row["note"] in (Engine.CHOSEN_NOTE,
                                          Engine.CHOSEN_NONE_NOTE)
        if shots:
            first_t = min(s["t"] or 0 for s in shots)
            why = ("YOU MOVED THESE %d SCREENSHOTS HERE" % len(shots) if moved
                   else "YOU CHOSE THESE %d SCREENSHOTS" % len(shots) if chosen
                   else "MATCHED AUTOMATICALLY - %d screenshot%s, the first "
                        "taken %s" % (len(shots), "" if len(shots) == 1 else "s",
                                      _vs_battle(first_t, t_close)
                                      or "at %s" % _clock(first_t)))
            if len(shots) > app.s["max_shots"]:
                why += " (only the first %d are sent)" % app.s["max_shots"]
        else:
            why = ("NO SCREENSHOTS - your choice" if chosen else
                   "NO SCREENSHOTS - Upload sends the battle without them")
        _label(sec, why, size=8, bold=True, fg=AMBER, bg=PANEL, anchor="w",
               wraplength=580).pack(fill="x")
        if shots:
            th = _ScaledThumbs(sec, [
                (s["path"], "%s  %s" % (_clock(s["t"] or 0),
                                        _vs_battle(s["t"] or 0, t_close)))
                for s in shots], cache)
            th.frame.pack(fill="x", pady=(6, 0))

        pick = tk.Frame(sec, bg=PANEL)
        pick.pack(fill="x", pady=(6, 0))
        _button(pick, "Change screenshots..." if shots else
                "Choose screenshots...",
                lambda: _pick_shots(r, app, row,
                                    lambda: (refresh(force=True), changed()))
                ).pack(side="left")
        others = [o for o in waiting if o["md5"] != md5]
        if others and shots:
            lut = {app.eng._label(o): o["md5"] for o in others}
            var = tk.StringVar(r, value="give the screenshots to...")

            def move(choice, lut=lut):
                if choice in lut:
                    act(lambda m: app.eng.reassign(m, lut[choice]), md5)()

            ttk.OptionMenu(pick, var, "give the screenshots to...",
                           *lut.keys(), command=move).pack(side="left", padx=6)

        btns = tk.Frame(box, bg=PANEL)
        btns.pack(fill="x", padx=10, pady=(10, 10))
        _button(btns, "Upload", act(app.eng.confirm, md5),
                primary=True).pack(side="left")
        if shots:
            _button(btns, "Upload without screenshots",
                    act(app.eng.upload_without_shots, md5)).pack(
                side="left", padx=(6, 0))
        _button(btns, "Skip", act(app.eng.skip, md5)).pack(side="left", padx=6)

    def waiting_card(pg, row):
        """A battle the matcher is still waiting on. It used to be only a
        count until it was held, and with no scorecards that is 15 minutes
        (orphan_after) - long enough to look like the window had stopped
        updating (Stargatecraft, PR #6). It can be settled from here."""
        md5 = row["md5"]
        t_close = row["t_close"] or 0
        box = tk.Frame(pg, bg=PANEL, highlightbackground=LINE,
                       highlightthickness=1)
        box._waiting = True
        box.pack(fill="x", pady=6)
        _label(box, app.eng._label(row), size=11, bold=True,
               bg=PANEL, anchor="w", wraplength=580).pack(
            fill="x", padx=10, pady=(8, 0))
        if row["note"]:
            _label(box, row["note"], size=9, fg=MUTED, bg=PANEL, anchor="w",
                   wraplength=580).pack(fill="x", padx=10)
        if t_close:
            _label(box, "Replay saved at %s" % _clock(t_close), size=8,
                   fg=MUTED, bg=PANEL, anchor="w", wraplength=580).pack(
                fill="x", padx=10)
        if row["backfill"] or app.s.get("review_mode"):
            then = "it comes here for you to decide"
        elif app.s.get("upload_without_scorecards"):
            then = "it uploads without them"
        else:
            then = "it is kept and not uploaded"
        when = (_clock(t_close + app.s["orphan_after"]) if t_close
                else "a while")
        _label(box, "WAITING FOR SCORECARDS - if none are taken by %s, or "
                    "you play %d more battles first, %s"
               % (when, app.s["orphan_after_n_later"], then),
               size=8, bold=True, fg=AMBER, bg=PANEL, anchor="w",
               wraplength=580).pack(fill="x", padx=10, pady=(8, 0))
        btns = tk.Frame(box, bg=PANEL)
        btns.pack(fill="x", padx=10, pady=(10, 10))
        _button(btns, "Choose screenshots...",
                lambda: _pick_shots(r, app, row,
                                    lambda: (refresh(force=True), changed()))
                ).pack(side="left")
        _button(btns, "Upload without screenshots",
                act(app.eng.upload_without_shots, md5)).pack(
            side="left", padx=(6, 0))
        _button(btns, "Skip", act(app.eng.skip, md5)).pack(side="left", padx=6)

    def render(held, waiting, shots, settle=(0, None)):
        top = body.canvas.yview()[0]
        was_empty = page[0] is not None and not getattr(page[0], "_held", 0)
        if page[0] is not None:
            page[0].destroy()
        pg = page[0] = tk.Frame(body.inner, bg=BG)
        pg._held = len(held) + len(waiting)
        pg.pack(fill="x")
        _set_title(r, _review_title(held))
        body.center = not held and not waiting
        n_settle, settle_at = settle
        settle_text = ("%d screenshot%s just taken - %s matched to a battle "
                       "at %s, in case you take another."
                       % (n_settle, "" if n_settle == 1 else "s",
                          "it is" if n_settle == 1 else "they are",
                          _clock(settle_at))) if n_settle else ""
        if not held and not waiting:
            _label(pg, "Nothing to review.", size=13, bold=True,
                   wraplength=520, justify="center").pack()
            _label(pg, (settle_text or "Everything it has seen has been "
                                       "dealt with.") +
                   " Leave this open: anything that needs you appears here.",
                   size=9, fg=MUTED, wraplength=520,
                   justify="center").pack(pady=(8, 0))
            _button(pg, "Close", r.destroy).pack(pady=16)
        else:
            if was_empty and r.winfo_height() < _px(480):
                # it opened with nothing to show; make room for a panel
                r.geometry("%dx%d" % (r.winfo_width(),
                                      min(_px(620), r.winfo_screenheight() - 80)))
            if not held:
                _label(pg, "Nothing to review.", size=12, bold=True,
                       anchor="w").pack(fill="x")
            for row in held:
                card(pg, row, shots[row["md5"]], waiting)
            if waiting or settle_text:
                _label(pg, "STILL WAITING", size=8, bold=True, fg=MUTED,
                       anchor="w").pack(fill="x", pady=(12 if held else 8, 0))
                if settle_text:
                    _label(pg, settle_text, size=9, fg=MUTED, anchor="w",
                           wraplength=600).pack(fill="x", pady=(2, 0))
                for row in waiting:
                    waiting_card(pg, row)
        for p in list(cache):
            if not any(p == s["path"] for v in shots.values() for s in v):
                del cache[p]
        body._layout()
        r.after_idle(lambda: body.canvas.winfo_exists()
                     and body.canvas.yview_moveto(top))
        # No gc.collect() here: it would also collect windows already closed
        # on OTHER threads, which is the very crash _thread() avoids. The
        # old panels go with this thread's collection when the window closes.

    def refresh(force=False, snap=None):
        held, waiting, shots, settle, sig = snap or snapshot()
        if force or sig != last[0]:
            last[0] = sig
            render(held, waiting, shots, settle)

    def front():
        try:
            r.deiconify()
            r.lift()
            r.attributes("-topmost", True)
            r.after(300, lambda: r.winfo_exists()
                    and r.attributes("-topmost", False))
            r.focus_force()
        except Exception:
            pass

    # A short step so a second tray click brings the window forward at once;
    # the store is re-read only every refresh_ms.
    ticks = [0]
    step = 250

    def tick():
        if _REVIEW["front"]:
            _REVIEW["front"] = False
            front()
        ticks[0] += step
        if ticks[0] >= refresh_ms:
            ticks[0] = 0
            refresh()

    refresh(snap=first)
    _poll(r, step, tick, app.log, "review window")
    r.mainloop()


# -------------------------------------------------------------- settings ----

def open_settings(app):
    def build():
        import tkinter as tk
        from tkinter import filedialog, ttk

        s = app.s
        r = _root("Debrief Uploader - settings", 640, 720)
        # The sections are taller than a 720 px window once Windows display
        # scaling applies (a tester at 150% saw it end at the review checkbox,
        # Save and Startup cut off, so nothing he changed was kept). The body
        # scrolls, the window fits the screen, and every change saves itself.
        _set_size(r, min(_px(640), r.winfo_screenwidth() - 40),
                  max(min(_px(480), r.winfo_screenheight() - 120),
                      min(_px(900), r.winfo_screenheight() - 120)))
        # the footer first, so it keeps its place as the window shrinks and
        # the body scrolls instead
        foot = tk.Frame(r, bg=BG)
        foot.pack(side="bottom", fill="x", padx=(16, _gutter()), pady=(4, 12))
        outer = _Body(r).inner

        status = _label(foot, "Changes save as you make them.", size=9, fg=MUTED)

        # ---- first run: nothing is read until this is pressed ----
        if not s.get("watching_confirmed"):
            gate = tk.Frame(outer, bg=PANEL, highlightbackground=AMBER,
                            highlightthickness=1)
            gate.pack(fill="x", pady=(0, 10))
            _label(gate, "CHECK THESE FOLDERS FIRST", size=8, fg=AMBER,
                   bold=True, bg=PANEL).pack(anchor="w", padx=12, pady=(9, 4))
            _label(gate, "Nothing has been read yet. Look over the folders "
                         "below and add anything that must never be "
                         "uploaded (sensitive folders) to "
                         "Excluded folders. Then press Start watching.\n\n"
                         "Only battles and screenshots from after that "
                         "moment are ever considered - anything already in "
                         "these folders is left alone.",
                   size=9, bg=PANEL, wraplength=560).pack(
                anchor="w", padx=12)

            def start_watching():
                s.confirm_watching()
                try:
                    app.eng.apply_exclusions()
                except Exception as e:
                    app.log.error("could not apply exclusions: %s" % e)
                app.log.info("watching started - only files from now on "
                             "are considered")
                gate.destroy()
                status.configure(text="Watching started.", fg=AMBER)
                refresh = getattr(app, "_refresh", None)
                if refresh:
                    try:
                        refresh()
                    except Exception:
                        pass

            _button(gate, "Start watching", start_watching,
                    primary=True).pack(anchor="w", padx=12, pady=(8, 11))

        # ---- account ----
        acc = _section(outer, "Account")
        who = _label(acc, "", size=10, bg=PANEL)
        who.pack(anchor="w", padx=12)
        accrow = tk.Frame(acc, bg=PANEL)
        accrow.pack(anchor="w", padx=12, pady=(6, 11))

        def refresh_account():
            if app.client.session.signed_in:
                who.configure(text="Signed in as %s"
                              % (app.client.session.email or "your account"))
            else:
                who.configure(text="Not signed in - nothing will upload")

        def do_sign_in():
            open_sign_in(app, on_done=refresh_account)

        def do_sign_out():
            app.client.sign_out()
            refresh_account()

        _button(accrow, "Sign in...", do_sign_in).pack(side="left")
        _button(accrow, "Sign out", do_sign_out).pack(side="left", padx=6)
        refresh_account()

        # ---- folders ----
        def folder_box(title, key, hint, on_change=None):
            box = _section(outer, title)
            _label(box, hint, size=8, fg=MUTED, bg=PANEL,
                   wraplength=560).pack(anchor="w", padx=12)
            lst = tk.Listbox(box, bg=BG, fg=INK, relief="flat", height=3,
                             highlightthickness=0, selectbackground="#1d3448",
                             font=("Consolas", 8))
            lst.pack(fill="x", padx=12, pady=(6, 4))
            for d in s[key]:
                lst.insert("end", d)

            def add():
                d = filedialog.askdirectory(title="Choose a folder")
                if d:
                    d = os.path.normpath(d)
                    if d not in s[key]:
                        s[key].append(d)
                        s["_dirs_pinned"] = True
                        lst.insert("end", d)
                        save_now()
                        if on_change:
                            on_change()

            def remove():
                for i in reversed(lst.curselection()):
                    s[key].pop(i)
                    lst.delete(i)
                s["_dirs_pinned"] = True
                save_now()
                if on_change:
                    on_change()

            row = tk.Frame(box, bg=PANEL)
            row.pack(anchor="w", padx=12, pady=(0, 11))
            _button(row, "Add...", add).pack(side="left")
            _button(row, "Remove selected", remove).pack(side="left", padx=6)
            return lst

        folder_box("Replay folders", "replay_dirs",
                   "Where the game saves battles. It keeps only ~10, so these "
                   "are copied somewhere safe the moment they appear.")
        folder_box("Screenshot folders", "shot_dirs",
                   "Where your scorecard captures land. Windows: "
                   "Pictures\\Screenshots. Steam F12: the Steam userdata "
                   "screenshots folder.")

        def exclusions_changed():
            try:
                n = app.eng.apply_exclusions()
            except Exception as e:
                app.log.error("could not apply exclusions: %s" % e)
                return
            if n:
                status.configure(text="Dropped %d battle(s) from excluded "
                                      "folders." % n, fg=AMBER)

        folder_box("Excluded folders", "exclude_dirs",
                   "Never read anything inside these, even when it sits in a "
                   "folder above. For replays or screenshots you must not "
                   "share, such as sensitive folders. Anything "
                   "already waiting from an excluded folder is dropped; "
                   "battles already uploaded stay on the site until you "
                   "delete them there.", on_change=exclusions_changed)

        # ---- uploads ----
        up = _section(outer, "Uploads")
        _label(up, "Visibility for new uploads", size=9, bg=PANEL).pack(
            anchor="w", padx=12)
        vis = tk.StringVar(r)
        current = s.get("visibility") or ""
        vis.set(next(lbl for lbl, v in VISIBILITY if v == current))
        ttk.OptionMenu(up, vis, vis.get(),
                       *[lbl for lbl, _ in VISIBILITY]).pack(
            anchor="w", padx=12, pady=(3, 8))

        _label(up, "Training-room battles", size=9, bg=PANEL).pack(
            anchor="w", padx=12)
        tvis = tk.StringVar(r)
        _tcur = s.get("training_visibility")
        _tcur = "" if _tcur is None else _tcur
        tvis.set(next((lbl for lbl, v in TRAINING_VISIBILITY if v == _tcur),
                      TRAINING_VISIBILITY[0][0]))
        ttk.OptionMenu(up, tvis, tvis.get(),
                       *[lbl for lbl, _ in TRAINING_VISIBILITY]).pack(
            anchor="w", padx=12, pady=(3, 4))
        _label(up, "Practice against bots, with no scorecard to add to the "
                   "research. Private by default.",
               size=8, fg=MUTED, bg=PANEL, wraplength=560).pack(
            anchor="w", padx=12, pady=(0, 8))

        review = tk.BooleanVar(r, value=bool(s.get("review_mode")))
        tk.Checkbutton(up, text="Ask me before every upload (review mode)",
                       variable=review, command=lambda: save_now(), bg=PANEL, fg=INK, selectcolor=BG,
                       activebackground=PANEL, activeforeground=INK,
                       font=("Segoe UI", 9)).pack(anchor="w", padx=9)
        _label(up, "Worth turning on for your first session, so you can watch "
                   "it pair correctly before trusting it.",
               size=8, fg=MUTED, bg=PANEL, wraplength=560).pack(
            anchor="w", padx=12, pady=(0, 11))

        # ---- screenshots ----
        sc = _section(outer, "Screenshot scanning")
        grid = tk.Frame(sc, bg=PANEL)
        grid.pack(anchor="w", padx=12, pady=(2, 4))
        _label(grid, "Only look at screenshots from the last", size=9,
               bg=PANEL).grid(row=0, column=0, sticky="w")
        age = tk.Spinbox(grid, from_=1, to=168, width=5, bg=BG, fg=INK,
                         relief="flat", buttonbackground="#16283a",
                         font=("Segoe UI", 9))
        age.delete(0, "end")
        age.insert(0, str(s["shot_max_age_hours"]))
        age.grid(row=0, column=1, padx=6)
        _label(grid, "hours", size=9, bg=PANEL).grid(row=0, column=2, sticky="w")

        _label(grid, "Ignore images smaller than", size=9, bg=PANEL).grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        minkb = tk.Spinbox(grid, from_=0, to=20000, increment=100, width=5,
                           bg=BG, fg=INK, relief="flat",
                           buttonbackground="#16283a", font=("Segoe UI", 9))
        minkb.delete(0, "end")
        minkb.insert(0, str(s["shot_min_kb"]))
        minkb.grid(row=1, column=1, padx=6, pady=(6, 0))
        _label(grid, "KB", size=9, bg=PANEL).grid(row=1, column=2, sticky="w",
                                                  pady=(6, 0))
        _label(sc, "Only skips small images without opening them. A full-"
                   "screen capture, PNG or JPEG, is well over 100 KB.",
               size=8, fg=MUTED, bg=PANEL, wraplength=560).pack(
            anchor="w", padx=12, pady=(4, 11))

        # ---- startup ----
        st = _section(outer, "Startup")
        auto = tk.BooleanVar(r, value=autostart.is_enabled())
        auto_msg = _label(st, "", size=8, fg=MUTED, bg=PANEL, wraplength=560)

        def toggle_auto():
            ok, why = (autostart.enable() if auto.get() else autostart.disable())
            if not ok:
                auto.set(not auto.get())
            auto_msg.configure(text=why)

        cb = tk.Checkbutton(st, text="Start automatically when I log in",
                            variable=auto, command=toggle_auto, bg=PANEL,
                            fg=INK, selectcolor=BG, activebackground=PANEL,
                            activeforeground=INK, font=("Segoe UI", 9))
        cb.pack(anchor="w", padx=9)
        if not autostart.supported():
            cb.configure(state="disabled")
            auto_msg.configure(text="Only available on Windows.")
        auto_msg.pack(anchor="w", padx=12, pady=(0, 11))

        # ---- saving: every change, as it happens ----
        def save_now(*_):
            s["visibility"] = next(v for lbl, v in VISIBILITY
                                   if lbl == vis.get()) or None
            s["training_visibility"] = next(
                v for lbl, v in TRAINING_VISIBILITY if lbl == tvis.get())
            s["review_mode"] = bool(review.get())
            try:
                s["shot_max_age_hours"] = max(1, int(age.get()))
                s["shot_min_kb"] = max(0, int(minkb.get()))
            except ValueError:
                status.configure(text="Those numbers need to be whole numbers; "
                                      "everything else is saved.", fg="#f0a173")
                s.save()
                return
            s.save()
            app.log.info("settings saved")
            status.configure(text="Saved.", fg=AMBER)

        vis.trace_add("write", save_now)
        tvis.trace_add("write", save_now)
        for sp in (age, minkb):
            sp.configure(command=save_now)
            sp.bind("<FocusOut>", save_now)
            sp.bind("<Return>", save_now)

        def close():
            save_now()
            r.destroy()

        r.protocol("WM_DELETE_WINDOW", close)
        status.pack(side="left")
        _button(foot, "Done", close, primary=True).pack(side="right")
        r.mainloop()

    _thread(build, app.log, "the settings window")
