"""System tray icon and the review window.

Deliberately thin. Every decision lives in engine.py, which the CLI drives just
as well -- so if the tray misbehaves on a particular PC, `run` without --tray
is a complete fallback and `review` resolves anything waiting.

Needs pystray. tkinter ships with Python on Windows.
"""
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser

from . import config, matcher, ui
from .api import ApiError
from .engine import Engine
from .ui import _res

IDLE = (0x64, 0x7d, 0x8e)
WATCH = (0x0f, 0x6b, 0x70)
BUSY = (0xff, 0xd1, 0x66)
ATTN = (0xa8, 0x41, 0x0e)

# True: right click opens our own themed popup. False: native Windows menu
# (dark, with the logo in front of the title). Needs pystray==0.19.5, because
# both variants reach into private pystray members.
CUSTOM_MENU = True

_BASE = None
_NO_LOGO = object()


def _base_logo(size):
    """The app logo at `size` px, or None if the icon file can't be read.

    A multi-size .ico opens at its largest image (256 px), so every size is
    a downscale.
    """
    global _BASE
    from PIL import Image
    if _BASE is None:
        try:
            # The tray gets its own art: the helmet alone, cropped to fill the
            # square. app.ico (exe, installer, windows) carries the bubbles
            # and padding, which made the tray icon read small beside its
            # neighbours (Greg 2026-10-11). Falls back to app.ico.
            try:
                src = Image.open(_res("tray.png"))
            except Exception:
                src = Image.open(_res("app.ico"))
            with src:
                _BASE = src.convert("RGBA")
        except Exception:
            _BASE = _NO_LOGO
    if _BASE is _NO_LOGO:
        return None
    return _BASE.resize((size, size), Image.LANCZOS)


def _drawn_mark(colour, size=64):
    """The original runtime-drawn mark. Used when the logo file is missing,
    so the tray icon is never a transparent square nobody can find."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([4, 4, 60, 60], fill=(11, 22, 32, 255), outline=colour + (255,), width=5)
    d.polygon([(32, 18), (44, 44), (32, 37), (20, 44)], fill=colour + (255,))
    return im


def _icon_image(colour):
    """App logo fills the canvas, status dot in the top right corner."""
    from PIL import Image, ImageDraw
    size = 64
    scale = 4
    logo = _base_logo(size)
    if logo is None:
        return _drawn_mark(colour, size)
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # 1. Logo first, 64 px, at x=0, y=0
    im.alpha_composite(logo, (0, 0))

    # 2. Status dot on top, tucked into the top-right corner: radius 9 px,
    # center (54, 10), with a 2 px dark ring so it reads as a badge over the
    # helmet, which now fills the canvas.
    dot = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    cx, cy, r, ring = 54 * scale, 10 * scale, 9 * scale, 2 * scale
    d = ImageDraw.Draw(dot)
    d.ellipse((cx - r - ring, cy - r - ring, cx + r + ring, cy + r + ring),
              fill=(11, 22, 32, 255))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=colour + (255,))
    im.alpha_composite(dot.resize((size, size), Image.LANCZOS))

    return im


# ---------------------------------------------------------- native menu -----

def _style_tray_menu():
    """Dark native menus and the logo in front of the title item. Windows only.

    This is the fallback when CUSTOM_MENU is False or the custom popup fails.
    """
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    # 1. dark menus: 2 = always dark, 1 = follow the system setting
    try:
        ux = ctypes.WinDLL("uxtheme")
        ux[135](2)      # SetPreferredAppMode
        ux[136]()       # FlushMenuThemes
    except Exception:
        pass

    # 2. logo bitmap
    try:
        from PIL import Image
        from pystray import _win32 as pw
        import pystray

        px = ctypes.windll.user32.GetSystemMetrics(49)    # SM_CXSMICON
        logo = _base_logo(px)
        if logo is None:
            return

        class BMIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD),
                        ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG),
                        ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        hdr = BMIH(ctypes.sizeof(BMIH), px, -px, 1, 32, 0, 0, 0, 0, 0, 0)
        gdi = ctypes.windll.gdi32
        gdi.CreateDIBSection.restype = ctypes.c_void_p
        bits = ctypes.c_void_p()
        hbmp = gdi.CreateDIBSection(None, ctypes.byref(hdr), 0,
                                    ctypes.byref(bits), None, 0)
        if not hbmp:
            return
        r, g, b, a = logo.convert("RGBa").split()      # premultiplied alpha
        data = Image.merge("RGBA", (b, g, r, a)).tobytes()
        ctypes.memmove(bits, data, len(data))

        original = pw.Icon._create_menu_item

        def with_logo(self, descriptor, callbacks):
            info = original(self, descriptor, callbacks)
            if (descriptor is not pystray.Menu.SEPARATOR
                    and descriptor.text.startswith("Debrief Uploader")):
                info.fMask |= 0x80          # MIIM_BITMAP
                info.hbmpItem = hbmp
                info.fState = (info.fState & ~0x3) | 0x2   # disabled, not grayed
            return info

        pw.Icon._create_menu_item = with_logo
    except Exception:
        pass


# ---------------------------------------------------------- custom menu -----

def _work_area_at(x, y):
    """Usable area (without taskbar) of the monitor at this point."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32

    class MI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    u.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    u.MonitorFromPoint.restype = ctypes.c_void_p
    u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MI)]
    mon = u.MonitorFromPoint(wintypes.POINT(x, y), 2)
    mi = MI()
    mi.cbSize = ctypes.sizeof(MI)
    u.GetMonitorInfoW(mon, ctypes.byref(mi))
    w = mi.rcWork
    return w.left, w.top, w.right, w.bottom


class _PopupHost:
    """One hidden Tk root on one long lived thread, shared by every popup.

    A new tk.Tk() per right click crashed the app on the second open: the
    first root, its fonts and its image were still waiting for the garbage
    collector, and when that ran on another thread Tcl aborted the whole
    process. Here the root, the fonts and the logo image are created once and
    never freed. Each popup is only a Toplevel that is destroyed on the same
    thread that made it.
    """

    def __init__(self):
        self.q = queue.Queue()
        self.open = False
        self.ready = False
        threading.Thread(target=self._run, daemon=True).start()

    def request(self, icon, x, y):
        self.q.put((icon, x, y))

    def p(self, n):
        return int(round(n * self.k))

    def _run(self):
        import tkinter as tk
        import tkinter.font as tkfont
        try:
            root = tk.Tk()
            root.withdraw()
            # Other windows must not pick this root up as the default root.
            if tk._default_root is root:
                tk._default_root = None
            self.root = root
            self.k = max(1.0, root.winfo_fpixels("1i") / 96.0)
            self.font = tkfont.Font(root=root, family="Segoe UI", size=10)
            self.bold = tkfont.Font(root=root, family="Segoe UI", size=10,
                                    weight="bold")
            self.photo = None
            logo = _base_logo(self.p(18))
            if logo is not None:
                from PIL import ImageTk
                self.photo = ImageTk.PhotoImage(logo, master=root)
            self.ready = True
        except Exception as e:
            print("tray menu:", e)
            return

        def poll():
            try:
                while True:
                    icon, x, y = self.q.get_nowait()
                    if self.open:
                        continue
                    self.open = True
                    try:
                        self._show(icon, x, y)
                    except Exception as e:
                        print("tray menu:", e)
                        self.open = False
            except queue.Empty:
                pass
            root.after(40, poll)

        poll()
        root.mainloop()

    def _show(self, icon, cx, cy):
        import ctypes
        import tkinter as tk
        from ctypes import wintypes
        import pystray

        items = [None if i is pystray.Menu.SEPARATOR else i
                 for i in icon.menu]
        if not items:
            self.open = False
            return

        p, font, bold, photo = self.p, self.font, self.bold, self.photo
        top = tk.Toplevel(self.root)
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)

        BGM, HOVER, SEPC = ui.PANEL, "#1d3448", ui.LINE
        ROW, SEP, PADX, PADY, RAD = p(34), p(9), p(18), p(6), p(6)
        ico_w = (p(18) + p(10)) if photo else 0

        def fnt(it):
            return bold if it.default else font

        width = p(220)
        for n, it in enumerate(items):
            if it is not None:
                need = fnt(it).measure(it.text) + (ico_w if n == 0 else 0)
                width = max(width, need + 2 * PADX + 2 * PADY)

        rows, y = [], PADY
        for it in items:
            step = SEP if it is None else ROW
            rows.append((it, y, y + step))
            y += step
        height = y + PADY

        # above the cursor like the native menu, inside the work area
        left, upper, right, bottom = _work_area_at(cx, cy)
        gap = p(8)
        x0 = max(left + gap, min(cx - width, right - width - gap))
        y0 = max(upper + gap, min(cy - height, bottom - height - gap))

        c = tk.Canvas(top, width=width, height=height, bg=BGM,
                      highlightthickness=0, bd=0)
        c.pack()

        def rrect(a, b, e, d, rad, **kw):
            pts = [a + rad, b, e - rad, b, e, b, e, b + rad, e, d - rad, e, d,
                   e - rad, d, a + rad, d, a, d, a, d - rad, a, b + rad, a, b]
            return c.create_polygon(pts, smooth=True, **kw)

        for n, (it, a, b) in enumerate(rows):
            if it is None:
                m = (a + b) // 2
                c.create_line(PADX, m, width - PADX, m, fill=SEPC)
                continue
            tx = PADX
            if n == 0 and photo:
                c.create_image(PADX, (a + b) // 2, image=photo, anchor="w")
                tx += ico_w
            c.create_text(tx, (a + b) // 2, text=it.text, anchor="w",
                          font=fnt(it),
                          fill=ui.INK if it.enabled else ui.MUTED)

        state = {"i": None, "after": None, "done": False}

        def hover(i):
            if i == state["i"]:
                return
            state["i"] = i
            c.delete("hl")
            if i is not None:
                _, a, b = rows[i]
                rrect(PADY, a, width - PADY, b, RAD, fill=HOVER, outline="",
                      tags="hl")
                c.tag_lower("hl")

        def hit(yy):
            for i, (it, a, b) in enumerate(rows):
                if it is not None and it.enabled and a <= yy < b:
                    return i
            return None

        def close(*_):
            if state["done"]:
                return
            state["done"] = True
            if state["after"]:
                try:
                    top.after_cancel(state["after"])
                except Exception:
                    pass
            try:
                top.destroy()
            except tk.TclError:
                pass
            self.open = False

        def activate(i):
            if i is None or state["done"]:
                return
            it = rows[i][0]
            close()
            threading.Thread(target=icon._handler(it), daemon=True).start()

        def step_key(d):
            order = [i for i, (it, _, _) in enumerate(rows)
                     if it is not None and it.enabled]
            if not order:
                return
            cur = state["i"]
            nxt = (order[0] if cur not in order
                   else order[(order.index(cur) + d) % len(order)])
            hover(nxt)

        c.bind("<Motion>", lambda e: hover(hit(e.y)))
        c.bind("<Leave>", lambda e: hover(None))
        c.bind("<ButtonRelease-1>", lambda e: activate(hit(e.y)))

        top.geometry("%dx%d+%d+%d" % (width, height, x0, y0))
        top.update_idletasks()

        # A private user32 handle, so these argtypes never touch the ones
        # other modules set on ctypes.windll.user32.
        u = ctypes.WinDLL("user32")
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetAncestor.restype = wintypes.HWND
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetAsyncKeyState.restype = ctypes.c_short
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int,
                                     ctypes.c_long]
        u.SetWindowLongW.restype = ctypes.c_long
        hwnd = u.GetAncestor(top.winfo_id(), 2)

        # Never take the foreground. The taskbar (also when it hides itself)
        # and the hidden icons flyout close as soon as they lose activation,
        # so the popup is a no-activate tool window and the taskbar keeps
        # the focus the right click gave it.
        try:
            style = u.GetWindowLongW(hwnd, -20)             # GWL_EXSTYLE
            u.SetWindowLongW(hwnd, -20,
                             style | 0x08000000 | 0x80)     # NOACTIVATE | TOOLWINDOW
        except Exception:
            pass

        try:
            from .winframe import _round_corners
            _round_corners(top, self.p)
        except Exception:
            pass
        top.deiconify()
        top.update_idletasks()

        # No focus means no key events, so Esc, Up, Down and Enter are read
        # from the keyboard state while the popup is open. Any click outside
        # and any change of the foreground window close it.
        keys = {0x1B: close,                                # Escape
                0x26: lambda: step_key(-1),                 # Up
                0x28: lambda: step_key(1),                  # Down
                0x0D: lambda: activate(state["i"])}         # Enter
        prev = {k: u.GetAsyncKeyState(k) < 0 for k in keys}
        fg0 = [None]

        def inside():
            pt = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            return x0 <= pt.x < x0 + width and y0 <= pt.y < y0 + height

        def watch():
            state["after"] = None
            if state["done"]:
                return
            try:
                if (any(u.GetAsyncKeyState(b) < 0 for b in (1, 2, 4))
                        and not inside()):
                    close()
                    return
                fg = u.GetForegroundWindow()
                if fg0[0] is None:
                    fg0[0] = fg       # whatever the right click activated
                elif fg != fg0[0]:
                    close()           # Alt+Tab, another window came up
                    return
                for k, fn in keys.items():
                    down = u.GetAsyncKeyState(k) < 0
                    fired = down and not prev[k]
                    prev[k] = down
                    if fired:
                        fn()
                        if state["done"]:
                            return
            except Exception:
                pass
            state["after"] = top.after(40, watch)

        state["after"] = top.after(150, watch)


_HOST = [None]


def _install_menus(custom=None):
    """Style / replace the tray menu, or leave pystray's own menu alone.

    Both variants patch PRIVATE pystray members (Icon._on_notify,
    Icon._create_menu_item). A pystray that renamed them must cost the user
    the pretty menu, never the tray: any failure here falls back to the
    native menu. Returns True when the custom popup is installed."""
    try:
        _style_tray_menu()
    except Exception as e:
        print("tray menu: native menu left unstyled (%s)" % e)
    if not (CUSTOM_MENU if custom is None else custom):
        return False
    try:
        _install_custom_menu()
        return True
    except Exception as e:
        print("tray menu: using the native menu (%s)" % e)
        return False


def _install_custom_menu():
    """Right click on the tray icon opens our popup instead of the native menu."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    from pystray import _win32 as pw

    original = pw.Icon._on_notify          # AttributeError if pystray changed
    if not hasattr(pw.Icon, "_handler"):
        raise AttributeError("pystray Icon has no _handler")
    _HOST[0] = _PopupHost()

    def on_notify(self, wparam, lparam):
        if lparam == pw.win32.WM_RBUTTONUP and self._menu_handle:
            try:
                pt = wintypes.POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                _HOST[0].request(self, pt.x, pt.y)
                return
            except Exception:
                pass
        return original(self, wparam, lparam)

    pw.Icon._on_notify = on_notify


def process_memory_mb():
    """Private memory of this process in MB (Windows: what Task Manager's
    "Commit size" shows), or peak resident size elsewhere. None if unknown."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD),
                            ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t),
                            ("PrivateUsage", ctypes.c_size_t)]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k = ctypes.WinDLL("kernel32")
            k.GetCurrentProcess.restype = wintypes.HANDLE
            k.GetCurrentProcess.argtypes = []
            fn = getattr(k, "K32GetProcessMemoryInfo", None) \
                or ctypes.WinDLL("psapi").GetProcessMemoryInfo
            # Declared, not guessed: the process handle is a 64-bit pseudo
            # handle (-1), which an undeclared call passes as a C int and
            # overflows -- the first build of this returned nothing on Windows.
            fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            fn.restype = wintypes.BOOL
            ok = fn(k.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
            return pmc.PrivateUsage / 1048576.0 if ok else None
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return r / (1048576.0 if sys.platform == "darwin" else 1024.0)
    except Exception:
        return None


class Tray:
    def __init__(self, settings, store, client, log):
        self.s = settings
        self.store = store
        self.client = client
        self.log = log
        self.eng = Engine(settings, store, client, log)
        self.paused = False
        self.icon = None
        self._stop = threading.Event()
        self._shown = None           # what the tray last displayed
        self._icons = {}             # colour -> icon image, built once each
        self._mem_beat = 0.0

    # ---- counts used by the menu -----------------------------------------
    def _counts(self):
        held = len(self.store.replays([matcher.HELD]))
        queued = len(self.store.replays([matcher.READY, matcher.UPLOADING]))
        waiting = len(self.store.replays([matcher.AWAITING_SHOTS]))
        return held, queued, waiting

    def _title(self):
        if not self.s.get("watching_confirmed"):
            return "Debrief Uploader - check your folders in Settings to start"
        if not self.client.session.signed_in:
            return "Debrief Uploader - not signed in"
        if self.eng.blocked:
            return "Debrief Uploader - %s" % self.eng.blocked
        if self.paused:
            return "Debrief Uploader - paused"
        held, queued, waiting = self._counts()
        bits = []
        if held:
            bits.append("%d need%s you" % (held, "s" if held == 1 else ""))
        if queued:
            bits.append("%d uploading" % queued)
        if waiting:
            bits.append("%d waiting for scorecards" % waiting)
        return "Debrief Uploader - " + (", ".join(bits) or "watching")

    def _colour(self):
        held, queued, _ = self._counts()
        if not self.s.get("watching_confirmed"):
            return ATTN
        if self.eng.blocked or not self.client.session.signed_in or held:
            return ATTN if held or self.eng.blocked else IDLE
        if queued:
            return BUSY
        return IDLE if self.paused else WATCH

    def _refresh(self):
        """Re-render the icon AND the menu.

        pystray.Menu is a snapshot of the items it was given. Building it once
        at startup and calling update_menu() afterwards re-renders the same
        frozen items, so the counts in the menu were whatever they had been
        when the app launched -- which is how a menu offering "Review (5)"
        opened a window correctly reporting that nothing needed reviewing.
        """
        if not self.icon:
            return
        # Only touch the tray when what it shows changed. This ran every poll
        # (2 s): a new icon image, a new ICO handle and a rebuilt native menu
        # each time, all day. A Windows install reached 16 GB and stopped
        # responding (2026-10-28); whatever the exact leak, churning native
        # handles 43,000 times a day for an unchanged icon is the place it
        # lives, and none of it is needed.
        colour, title = self._colour(), self._title()
        menu = self._build_menu()
        sig = (colour, title, tuple(
            (str(getattr(i, "text", i)), bool(getattr(i, "enabled", True)))
            for i in getattr(menu, "items", ())))
        if sig == self._shown:
            return
        if colour != (self._shown or (None,))[0]:
            if colour not in self._icons:
                self._icons[colour] = _icon_image(colour)
            self.icon.icon = self._icons[colour]
        self.icon.title = title
        self.icon.menu = menu
        self.icon.update_menu()
        self._shown = sig

    # ---- loop -------------------------------------------------------------
    def _loop(self):
        while not self._stop.is_set():
            if not self.paused:
                try:
                    self.eng.tick()
                except ApiError as e:
                    self.log.error(e.message)
                    if e.terminal:
                        self.eng.blocked = e.message
                except Exception as e:
                    self.log.error("unexpected: %s" % e)
            # _refresh touches the store and the tray API, both of which can
            # fail in ways that used to take the whole worker thread down with
            # them -- and a dead worker means nothing uploads, silently.
            try:
                self._refresh()
            except Exception as e:
                self.log.error("could not update the tray icon: %s" % e)
            self._log_memory()
            self._stop.wait(self.s["poll_interval"])

    MEM_EVERY = 300

    def _log_memory(self, now=None):
        """The process's memory every few minutes, so a slow leak shows up in
        the log with a time on it instead of as a frozen app."""
        now = now if now is not None else time.time()
        if now - self._mem_beat < self.MEM_EVERY:
            return
        self._mem_beat = now
        mb = process_memory_mb()
        if mb is not None:
            (self.log.error if mb > 1024 else self.log.info)(
                "memory: %.0f MB" % mb)


    # ---- menu actions -----------------------------------------------------
    def on_review(self, *_):
        ui.open_review(self, on_change=self._refresh)

    def on_settings(self, *_):
        ui.open_settings(self)

    def on_status(self, *_):
        ui.open_status(self)

    def on_doctor(self, *_):
        ui.open_doctor(self)

    def on_sign_in(self, *_):
        ui.open_sign_in(self, on_done=self._refresh)

    def on_check_now(self, *_):
        """Don't make someone wait out the poll interval to see if a change
        they just made worked."""
        def go():
            try:
                self.eng.tick()
            except Exception as e:
                self.log.error("check failed: %s" % e)
            self._refresh()
        threading.Thread(target=go, daemon=True).start()

    def on_open_site(self, *_):
        webbrowser.open(config.SITE_BASE + "/wowslegends/replays/")

    def on_open_logs(self, *_):
        d = config.log_dir()
        if os.name == "nt":
            os.startfile(d)                      # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", d], check=False)
        else:
            subprocess.run(["xdg-open", d], check=False)

    def on_pause(self, *_):
        self.paused = not self.paused
        (self.eng.pause if self.paused else self.eng.resume)()
        self.log.info("paused - battles and screenshots from now until you "
                      "resume are not uploaded unless you approve them"
                      if self.paused else "resumed")
        self._refresh()

    def on_retry(self, *_):
        self.eng.unblock()
        self._refresh()

    def on_quit(self, *_):
        self._stop.set()
        if self.icon:
            self.icon.stop()

    # ---- review window ----------------------------------------------------
    def _build_menu(self):
        """Built fresh every refresh -- the counts in it are live."""
        import pystray

        held, queued, waiting = self._counts()
        signed_in = self.client.session.signed_in
        items = [
            pystray.MenuItem(self._title(), None, enabled=False),
            pystray.Menu.SEPARATOR,
        ]
        confirmed = bool(self.s.get("watching_confirmed"))
        if not confirmed:
            items.append(pystray.MenuItem("Check folders and start...",
                                          self.on_settings, default=True))
        if not signed_in:
            # Nothing else matters until this is done, so it goes first and it
            # is the default action.
            items.append(pystray.MenuItem("Sign in...", self.on_sign_in,
                                          default=confirmed))
        else:
            items.append(pystray.MenuItem(
                "Review (%d)" % held, self.on_review,
                enabled=held > 0, default=held > 0))
        items += [
            pystray.MenuItem("Status...", self.on_status,
                             default=confirmed and signed_in and not held),
            pystray.MenuItem("Settings...", self.on_settings),
            pystray.MenuItem("Why isn't it uploading?", self.on_doctor),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Check now", self.on_check_now,
                             enabled=not self.paused),
            pystray.MenuItem("Open Debrief", self.on_open_site),
            pystray.MenuItem("Open log folder", self.on_open_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Resume" if self.paused else "Pause",
                             self.on_pause),
        ]
        if self.eng.blocked:
            items.append(pystray.MenuItem("Try again", self.on_retry))
        items += [pystray.Menu.SEPARATOR,
                  pystray.MenuItem("Quit", self.on_quit)]
        return pystray.Menu(*items)

    # ---- run --------------------------------------------------------------
    def run(self):
        if os.name == "nt":
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system-aware DPI scaling
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()  # pre-8.1 fallback
                except Exception:
                    pass
        try:
            import pystray
        except ImportError:
            self.log.error("the tray needs pystray (pip install pystray). "
                           "Run without --tray to use the console instead.")
            return 1

        # Both must run before pystray.Icon(...): pystray binds its private
        # handlers when the icon is created.
        _install_menus()

        self.icon = pystray.Icon("gd-debrief", _icon_image(self._colour()),
                                 self._title(), menu=self._build_menu())
        threading.Thread(target=self._loop, daemon=True).start()
        if not self.s.get("watching_confirmed"):
            # First run: put the folder check in front of the user rather
            # than waiting for them to find it in a tray menu.
            ui.open_settings(self)
        self.icon.run()
        self._stop.set()
        return 0


def run(settings, store, client, log):
    return Tray(settings, store, client, log).run()
