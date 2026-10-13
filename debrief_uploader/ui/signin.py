"""The Sign in window."""

import threading

from .. import oauth
from ..api import ApiError
from . import core
from .core import (
    BG, INK, MUTED, PANEL,
    _Body, _button, _label)


# --------------------------------------------------------------- sign in ----

def open_sign_in(app, on_done=None):
    def build():
        import tkinter as tk
        r = core._root("Sign in to GamingDiver", 420, 380)
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
    core._thread(build, app.log, "the sign-in window")
