"""Shared pieces of the uploader windows: the theme, window and widget
helpers, the scrolling body and the refresh loop.

Tests swap _root, _thread, _res and _SCALE here (ui.core.X); the window
modules call those four through `core.` so a swap reaches them."""

import os
import sys
import threading
import time


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
        from .. import winframe
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
    # resources/ sits beside the package's modules, one level above ui/
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
