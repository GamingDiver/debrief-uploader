"""The Review window: held and waiting battles, their screenshots, the
screenshot picker and the full-size viewer."""

import os
import threading

from .. import config
from . import core
from .core import (
    AMBER, BG, INK, LINE,
    MUTED, PANEL, _Body, _button,
    _clock, _gutter, _label, _poll,
    _px, _set_size, _set_title, _span,
    _toplevel, _work_area)


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
    from .. import watcher
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

    from .. import matcher

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
        core._thread(build, app.log, "the review window")
    except Exception:
        with _REVIEW_LOCK:
            _REVIEW["open"] = False
        raise


def _review_window(app, refresh_ms=1000):
    import tkinter as tk
    from tkinter import ttk

    from .. import matcher
    from ..engine import Engine

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
    r = core._root(_review_title(first[0]), 680,
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
        # on OTHER threads, which is the very crash core._thread() avoids. The
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
