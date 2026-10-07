"""Tray windows: settings, status, diagnosis, sign-in.

Everything here is a thin shell over the same code the CLI drives, so the tray
is a second front end rather than a second implementation. Each window opens on
its own thread with its own Tk root -- pystray owns the main thread -- and any
failure is logged with a pointer at the equivalent command, because a GUI that
cannot open must not become a dead end.
"""
import os
import threading

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
    threading.Thread(target=go, daemon=True).start()


def _root(title, w, h):
    import tkinter as tk
    r = tk.Tk()
    r.title(title)
    r.configure(bg=BG)
    r.geometry("%dx%d" % (w, h))
    return r


def _label(parent, text, size=10, fg=INK, bold=False, bg=BG, **kw):
    import tkinter as tk
    return tk.Label(parent, text=text, bg=bg, fg=fg, justify="left",
                    font=("Segoe UI", size, "bold" if bold else "normal"), **kw)


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


def _text_window(title, lines, log, what):
    def build():
        import tkinter as tk
        r = _root(title, 760, 560)
        t = tk.Text(r, bg=PANEL, fg=INK, insertbackground=INK, relief="flat",
                    font=("Consolas", 9), wrap="none", padx=12, pady=10)
        sb = tk.Scrollbar(r, command=t.yview, bg=BG)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        t.insert("1.0", "\n".join(lines))
        t.configure(state="disabled")
        r.mainloop()
    _thread(build, log, what)


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
        head.pack(fill="x", padx=16, pady=(14, 6))
        summary_lbl = _label(head, "", size=10, fg=MUTED)
        summary_lbl.pack(anchor="w")

        t = tk.Text(r, bg=PANEL, fg=INK, relief="flat", font=("Segoe UI", 10),
                    wrap="word", padx=14, pady=12, cursor="arrow",
                    highlightthickness=0)
        sb = tk.Scrollbar(r, command=t.yview, bg=BG)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(fill="both", expand=True, padx=(16, 0), pady=(0, 14))

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
            try:
                c, rows, sig = snapshot()
                if sig != last[0]:
                    last[0] = sig
                    render(c, rows)
            except Exception as e:   # a failed read must not kill the window
                app.log.error("status window refresh failed (%s)" % e)
            r.after(REFRESH_MS, tick)

        tick()
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
        pad = tk.Frame(r, bg=BG)
        pad.pack(fill="both", expand=True, padx=18, pady=16)
        _label(pad, "Use the same account as gamingdiver.com.", fg=MUTED,
               size=9).pack(anchor="w", pady=(0, 10))

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

        _label(pad, "Or with email and password:", fg=MUTED, size=9).pack(
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

def open_review(app, on_change=None):
    """Resolve the battles the matcher would not guess at.

    The title is derived from the CONTENT, not assumed. It used to read
    "needs you" over a window saying "Nothing needs you." -- which is the sort
    of small dishonesty that makes someone stop trusting the rest of it.
    """
    def build():
        import tkinter as tk
        from tkinter import ttk

        from . import matcher

        held = list(app.store.replays([matcher.HELD]))
        waiting = list(app.store.replays([matcher.AWAITING_SHOTS]))

        title = ("%s - %d to review" % (config.APP_NAME, len(held)) if held
                 else "%s - nothing to review" % config.APP_NAME)
        r = _root(title, 660, 540 if held else 300)

        if not held:
            pad = tk.Frame(r, bg=BG)
            pad.pack(expand=True, padx=24)
            _label(pad, "Nothing needs a decision.", size=13, bold=True).pack()
            if waiting:
                _label(pad, "%d battle%s waiting for scorecards - capture the "
                            "Personal and Team Result tabs and they will go up "
                            "on their own."
                       % (len(waiting), " is" if len(waiting) == 1 else "s are"),
                       size=9, fg=MUTED, wraplength=520).pack(pady=(8, 0))
            else:
                _label(pad, "Everything it has seen has been dealt with.",
                       size=9, fg=MUTED).pack(pady=(8, 0))
            _button(pad, "Close", r.destroy).pack(pady=16)
            r.mainloop()
            return

        frame = tk.Frame(r, bg=BG)
        frame.pack(fill="both", expand=True, padx=16, pady=16)
        thumbs = []                       # keep refs or Tk drops the images

        def changed():
            if on_change:
                on_change()

        for row in held:
            box = tk.Frame(frame, bg=PANEL, highlightbackground=LINE,
                           highlightthickness=1)
            box.pack(fill="x", pady=6)
            _label(box, app.eng._label(row), size=11, bold=True,
                   bg=PANEL, anchor="w").pack(fill="x", padx=10, pady=(8, 0))
            _label(box, row["note"] or "needs confirmation", size=9, fg=MUTED,
                   bg=PANEL, anchor="w", wraplength=580).pack(fill="x", padx=10)

            strip = tk.Frame(box, bg=PANEL)
            strip.pack(fill="x", padx=10, pady=6)
            for sh in app.store.shots_for(row["md5"])[:4]:
                try:
                    from PIL import Image, ImageTk
                    with Image.open(sh["path"]) as im:
                        im = im.convert("RGB")
                        im.thumbnail((150, 84))
                        ph = ImageTk.PhotoImage(im)
                except Exception:
                    continue
                thumbs.append(ph)
                tk.Label(strip, image=ph, bg=PANEL).pack(side="left", padx=(0, 6))

            btns = tk.Frame(box, bg=PANEL)
            btns.pack(fill="x", padx=10, pady=(0, 10))
            md5 = row["md5"]

            def upload(m=md5, b=box):
                app.eng.confirm(m)
                b.destroy()
                changed()

            def skip(m=md5, b=box):
                app.eng.skip(m)
                b.destroy()
                changed()

            _button(btns, "Upload", upload, primary=True).pack(side="left")
            _button(btns, "Skip", skip).pack(side="left", padx=6)

            others = [o for o in waiting if o["md5"] != md5]
            if others:
                lut = {app.eng._label(o): o["md5"] for o in others}
                var = tk.StringVar(value="give the screenshots to...")

                def move(choice, m=md5, lut=lut, b=box):
                    if choice in lut:
                        app.eng.reassign(m, lut[choice])
                        b.destroy()
                        changed()

                ttk.OptionMenu(btns, var, "give the screenshots to...",
                               *lut.keys(), command=move).pack(side="left", padx=6)

        r.mainloop()

    _thread(build, app.log, "the review window")


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
        r.geometry("640x%d" % max(480, min(900, r.winfo_screenheight() - 120)))
        foot = tk.Frame(r, bg=BG)
        foot.pack(side="bottom", fill="x", padx=16, pady=(4, 12))
        canvas = tk.Canvas(r, bg=BG, highlightthickness=0)
        vbar = tk.Scrollbar(r, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        outer = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((16, 14), window=outer, anchor="nw")
        outer.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=(0, 0, e.width + 32, e.height + 28)))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(
            win, width=max(200, e.width - 32)))

        def _wheel(e):
            canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")
        r.bind_all("<MouseWheel>", _wheel)

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
        vis = tk.StringVar()
        current = s.get("visibility") or ""
        vis.set(next(lbl for lbl, v in VISIBILITY if v == current))
        ttk.OptionMenu(up, vis, vis.get(),
                       *[lbl for lbl, _ in VISIBILITY]).pack(
            anchor="w", padx=12, pady=(3, 8))

        _label(up, "Training-room battles", size=9, bg=PANEL).pack(
            anchor="w", padx=12)
        tvis = tk.StringVar()
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

        review = tk.BooleanVar(value=bool(s.get("review_mode")))
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
        _label(sc, "Steam's F12 writes JPEG, which can be under 1000 KB at "
                   "1080p - lower this to about 200 if you capture that way.",
               size=8, fg=MUTED, bg=PANEL, wraplength=560).pack(
            anchor="w", padx=12, pady=(4, 11))

        # ---- startup ----
        st = _section(outer, "Startup")
        auto = tk.BooleanVar(value=autostart.is_enabled())
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
            r.unbind_all("<MouseWheel>")
            r.destroy()

        r.protocol("WM_DELETE_WINDOW", close)
        status.pack(side="left")
        _button(foot, "Done", close, primary=True).pack(side="right")
        r.mainloop()

    _thread(build, app.log, "the settings window")
