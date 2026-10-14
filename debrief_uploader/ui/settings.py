"""The Settings window."""

import os

from .. import autostart
from . import core
from .core import (
    AMBER, BG, INK, MUTED,
    PANEL, TRAINING_VISIBILITY, VISIBILITY, _Body,
    _button, _gutter, _label, _px,
    _section, _set_size)
from .signin import open_sign_in


# -------------------------------------------------------------- settings ----

def open_settings(app):
    """One Settings window: opening it again brings the open one forward."""
    def build(st):
        w = _SettingsWindow(app)
        core._watch_front(w.r, st, app.log, "settings window")
        w.run()
    core._single("settings", build, app.log, "the settings window")


class _SettingsWindow:
    """The Settings window: one method per section, every change saved as it
    happens (save_now). Built on the window's own thread by open_settings."""

    def __init__(self, app):
        import tkinter as tk

        self.app = app
        self.s = app.s
        r = self.r = core._root("Debrief Uploader - settings", 640, 720)
        # The sections are taller than a 720 px window once Windows display
        # scaling applies (a tester at 150% saw it end at the review checkbox,
        # Save and Startup cut off, so nothing he changed was kept). The body
        # scrolls, the window fits the screen, and every change saves itself.
        _set_size(r, min(_px(640), r.winfo_screenwidth() - 40),
                  max(min(_px(480), r.winfo_screenheight() - 120),
                      min(_px(900), r.winfo_screenheight() - 120)))
        # the footer first, so it keeps its place as the window shrinks and
        # the body scrolls instead
        self.foot = tk.Frame(r, bg=BG)
        self.foot.pack(side="bottom", fill="x", padx=(16, _gutter()), pady=(4, 12))
        self.outer = _Body(r).inner
        self.status = _label(self.foot, "Changes save as you make them.",
                             size=9, fg=MUTED)

        if not self.s.get("watching_confirmed"):
            self._first_run_gate()
        self._account()
        self._folders()
        self._uploads()
        self._screenshots()
        self._startup()
        self._wire_saving()

    def run(self):
        def close():
            self.save_now()
            self.r.destroy()

        self.r.protocol("WM_DELETE_WINDOW", close)
        self.status.pack(side="left")
        _button(self.foot, "Done", close, primary=True).pack(side="right")
        self.r.mainloop()

    # ---- first run: nothing is read until this is pressed ----
    def _first_run_gate(self):
        import tkinter as tk

        app, s = self.app, self.s
        gate = tk.Frame(self.outer, bg=PANEL, highlightbackground=AMBER,
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
            self.status.configure(text="Watching started.", fg=AMBER)
            refresh = getattr(app, "_refresh", None)
            if refresh:
                try:
                    refresh()
                except Exception:
                    pass

        _button(gate, "Start watching", start_watching,
                primary=True).pack(anchor="w", padx=12, pady=(8, 11))

    # ---- account ----
    def _account(self):
        import tkinter as tk

        app = self.app
        acc = _section(self.outer, "Account")
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
    def _folder_box(self, title, key, hint, on_change=None):
        import tkinter as tk
        from tkinter import filedialog

        s = self.s
        box = _section(self.outer, title)
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
                    self.save_now()
                    if on_change:
                        on_change()

        def remove():
            for i in reversed(lst.curselection()):
                s[key].pop(i)
                lst.delete(i)
            s["_dirs_pinned"] = True
            self.save_now()
            if on_change:
                on_change()

        row = tk.Frame(box, bg=PANEL)
        row.pack(anchor="w", padx=12, pady=(0, 11))
        _button(row, "Add...", add).pack(side="left")
        _button(row, "Remove selected", remove).pack(side="left", padx=6)
        return lst

    def _folders(self):
        app = self.app
        self._folder_box(
            "Replay folders", "replay_dirs",
            "Where the game saves battles. It keeps only ~10, so these "
            "are copied somewhere safe the moment they appear.")
        self._folder_box(
            "Screenshot folders", "shot_dirs",
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
                self.status.configure(text="Dropped %d battle(s) from excluded "
                                           "folders." % n, fg=AMBER)

        self._folder_box(
            "Excluded folders", "exclude_dirs",
            "Never read anything inside these, even when it sits in a "
            "folder above. For replays or screenshots you must not "
            "share, such as sensitive folders. Anything "
            "already waiting from an excluded folder is dropped; "
            "battles already uploaded stay on the site until you "
            "delete them there.", on_change=exclusions_changed)

    # ---- uploads ----
    def _uploads(self):
        import tkinter as tk
        from tkinter import ttk

        s, r = self.s, self.r
        up = _section(self.outer, "Uploads")
        _label(up, "Visibility for new uploads", size=9, bg=PANEL).pack(
            anchor="w", padx=12)
        self.vis = vis = tk.StringVar(r)
        current = s.get("visibility") or ""
        vis.set(next(lbl for lbl, v in VISIBILITY if v == current))
        ttk.OptionMenu(up, vis, vis.get(),
                       *[lbl for lbl, _ in VISIBILITY]).pack(
            anchor="w", padx=12, pady=(3, 8))

        _label(up, "Training-room battles", size=9, bg=PANEL).pack(
            anchor="w", padx=12)
        self.tvis = tvis = tk.StringVar(r)
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

        self.review = review = tk.BooleanVar(r, value=bool(s.get("review_mode")))
        tk.Checkbutton(up, text="Ask me before every upload (review mode)",
                       variable=review, command=lambda: self.save_now(), bg=PANEL, fg=INK, selectcolor=BG,
                       activebackground=PANEL, activeforeground=INK,
                       font=("Segoe UI", 9)).pack(anchor="w", padx=9)
        _label(up, "Worth turning on for your first session, so you can watch "
                   "it pair correctly before trusting it.",
               size=8, fg=MUTED, bg=PANEL, wraplength=560).pack(
            anchor="w", padx=12, pady=(0, 11))

    # ---- screenshots ----
    def _screenshots(self):
        import tkinter as tk

        s = self.s
        sc = _section(self.outer, "Screenshot scanning")
        grid = tk.Frame(sc, bg=PANEL)
        grid.pack(anchor="w", padx=12, pady=(2, 4))
        _label(grid, "Only look at screenshots from the last", size=9,
               bg=PANEL).grid(row=0, column=0, sticky="w")
        self.age = age = tk.Spinbox(grid, from_=1, to=168, width=5, bg=BG, fg=INK,
                                    relief="flat", buttonbackground="#16283a",
                                    font=("Segoe UI", 9))
        age.delete(0, "end")
        age.insert(0, str(s["shot_max_age_hours"]))
        age.grid(row=0, column=1, padx=6)
        _label(grid, "hours", size=9, bg=PANEL).grid(row=0, column=2, sticky="w")

        _label(grid, "Ignore images smaller than", size=9, bg=PANEL).grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        self.minkb = minkb = tk.Spinbox(grid, from_=0, to=20000, increment=100, width=5,
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
    def _startup(self):
        import tkinter as tk

        st = _section(self.outer, "Startup")
        auto = tk.BooleanVar(self.r, value=autostart.is_enabled())
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
        else:
            # Unticked for a reason the user should know (stale / Task Manager).
            auto_msg.configure(text=autostart.explain())
        auto_msg.pack(anchor="w", padx=12, pady=(0, 11))
        self.auto = auto

    # ---- saving: every change, as it happens ----
    def save_now(self, *_):
        s = self.s
        s["visibility"] = next(v for lbl, v in VISIBILITY
                               if lbl == self.vis.get()) or None
        s["training_visibility"] = next(
            v for lbl, v in TRAINING_VISIBILITY if lbl == self.tvis.get())
        s["review_mode"] = bool(self.review.get())
        try:
            s["shot_max_age_hours"] = max(1, int(self.age.get()))
            s["shot_min_kb"] = max(0, int(self.minkb.get()))
        except ValueError:
            self.status.configure(text="Those numbers need to be whole numbers; "
                                       "everything else is saved.", fg="#f0a173")
            s.save()
            return
        s.save()
        self.app.log.info("settings saved")
        self.status.configure(text="Saved.", fg=AMBER)

    def _wire_saving(self):
        self.vis.trace_add("write", self.save_now)
        self.tvis.trace_add("write", self.save_now)
        for sp in (self.age, self.minkb):
            sp.configure(command=self.save_now)
            sp.bind("<FocusOut>", self.save_now)
            sp.bind("<Return>", self.save_now)
