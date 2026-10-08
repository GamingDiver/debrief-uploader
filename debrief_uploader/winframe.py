"""Windows-only window chrome: our own title bar on top of the native frame.

The native frame is KEPT (WM_NCCALCSIZE hands the whole window to the client
area), so Snap, Alt+Tab, the taskbar, minimize/maximize animations and the
system menu keep working; only the title bar is drawn by us, in the app's
colours. Rounded corners come from DWM on Windows 11 and a window region on
Windows 10. Everything here is best-effort: any Win32 failure leaves the
plain window. Contributed by Stargatecraft (PR #6).

ui.py calls frame() + set_icon() before the window is shown and finish()
once it is placed.
"""
import ctypes
import sys

CLOSE = "\uE8BB"   # Segoe MDL2 Assets glyphs
MAXIMIZE, RESTORE, MINIMIZE = "\uE922", "\uE923", "\uE921"

# WNDPROC callbacks must outlive the window they are installed on, or Windows
# calls freed memory. A handful per session (one per window opened).
_KEEP = []


def finish(r, px):
    """After geometry is set: take over the title bar, round the corners and
    put the resize grips above everything packed since frame()."""
    r.update_idletasks()
    _native_frame(r)
    _round_corners(r, px)
    for g in getattr(r, "_grips", []):
        g.lift()


def _close(r):
    """The title bar's X does exactly what the native one would."""
    cmd = r.protocol("WM_DELETE_WINDOW")
    if cmd:
        r.tk.call(cmd)
    else:
        r.destroy()


def _native_frame(r):
    from ctypes import wintypes
    u = ctypes.windll.user32
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    u.GetParent.argtypes = [wintypes.HWND]
    u.GetParent.restype = wintypes.HWND
    u.IsZoomed.argtypes = [wintypes.HWND]
    u.GetDpiForWindow.argtypes = [wintypes.HWND]
    u.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wintypes.UINT]
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               wintypes.UINT]
    u.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                  wintypes.UINT, wintypes.WPARAM,
                                  wintypes.LPARAM]
    u.CallWindowProcW.restype = LRESULT
    setlong = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
    setlong.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    setlong.restype = ctypes.c_void_p

    hwnd = u.GetParent(r.winfo_id())
    if not hwnd:
        return
    old = [None]

    def proc(h, msg, wp, lp):
        try:
            if msg == 0x0083 and wp:                    # WM_NCCALCSIZE
                if u.IsZoomed(h):
                    dpi = u.GetDpiForWindow(h) or 96
                    pad = u.GetSystemMetricsForDpi(92, dpi)
                    fx = u.GetSystemMetricsForDpi(32, dpi) + pad
                    fy = u.GetSystemMetricsForDpi(33, dpi) + pad
                    rc = ctypes.cast(lp, ctypes.POINTER(wintypes.RECT)).contents
                    rc.left += fx
                    rc.top += fy
                    rc.right -= fx
                    rc.bottom -= fy
                return 0
        except Exception:
            pass
        return u.CallWindowProcW(old[0], h, msg, wp, lp)

    cb = WNDPROC(proc)
    prev = setlong(hwnd, -4, ctypes.cast(cb, ctypes.c_void_p))
    if not prev:
        return                       # hook failed, keep the normal frame
    old[0] = prev
    _KEEP.append(cb)
    u.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x37)


def frame(r, title, px, bg, icon, fg="#cfd8dc"):
    """Draw our own title bar (icon, title, min/max/close) and edge grips.
    Call before the window is shown; finish() completes it once mapped."""
    import tkinter as tk

    r.configure(highlightthickness=0)

    bar = tk.Frame(r, bg=bg, height=px(32))
    bar.pack(side="top", fill="x")
    bar.pack_propagate(False)

    grab = [bar]
    try:
        photo = _bar_icon(icon, px(18), r)
        ico = tk.Label(bar, bg=bg, image=photo)
        ico.image = photo
        ico.pack(side="left", padx=(px(10), 0))
        grab.append(ico)
    except Exception:
        pass

    label = tk.Label(bar, text=title, bg=bg, fg=fg, font=("Segoe UI", 9))
    label.pack(side="left", padx=px(8))
    grab.append(label)

    def toggle_max(e=None):
        r.state("normal" if r.state() == "zoomed" else "zoomed")

    btns = []

    def button(glyph, cmd, hover="#1b2f3d"):
        b = tk.Label(bar, text=glyph, bg=bg, fg=fg, width=5,
                     font=("Segoe MDL2 Assets", 8))
        b.pack(side="right", fill="y")
        btns.append(b)
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(
            bg=hover, fg="white" if hover != "#1b2f3d" else fg))
        b.bind("<Leave>", lambda e: b.configure(bg=bg, fg=fg))
        return b

    button(CLOSE, lambda: _close(r), hover="#c42b1c")   # close
    maxbtn = button(MAXIMIZE, toggle_max)                  # maximize
    button(MINIMIZE, r.iconify)                            # minimize

    def sync(e):
        if e.widget is r:
            maxbtn.configure(
                text=RESTORE if r.state() == "zoomed" else MAXIMIZE)
    r.bind("<Configure>", sync, add="+")

    def drag(e):
        try:
            u = ctypes.windll.user32
            u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                       ctypes.c_size_t, ctypes.c_ssize_t]
            hwnd = u.GetParent(r.winfo_id())
            u.ReleaseCapture()
            u.PostMessageW(hwnd, 0x0112, 0xF012, 0)   # WM_SYSCOMMAND, SC_MOVE | HTCAPTION
        except Exception:
            pass

    for wdg in grab:
        wdg.bind("<Button-1>", drag)
        wdg.bind("<Double-Button-1>", toggle_max)

    # ---- resize grips ----
    b = px(5)  # edge thickness
    c = px(10)  # corner size
    bh = px(32)  # title bar height
    r.update_idletasks()
    bw = sum(x.winfo_reqwidth() for x in btns)  # width of the three buttons
    r._grips = []

    def size_from(code):
        try:
            u = ctypes.windll.user32
            u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                       ctypes.c_size_t, ctypes.c_ssize_t]
            hwnd = u.GetParent(r.winfo_id())
            u.ReleaseCapture()
            u.PostMessageW(hwnd, 0x0112, 0xF000 + code, 0)
        except Exception:
            pass

    def grip(cursor, code, **where):
        g = tk.Frame(r, bg=bg, cursor=cursor)
        g.place(**where)
        g.bind("<Button-1>",
               lambda e: size_from(code) if r.state() != "zoomed" else None)
        r._grips.append(g)

    # left edge and top left corner
    grip("size_we", 1, x=0, y=c, width=b, relheight=1, height=-2 * c)
    grip("size_nw_se", 4, x=0, y=0, width=c, height=c)
    # top edge, stops before the buttons
    grip("size_ns", 3, x=c, y=0, relwidth=1, width=-(c + bw), height=b)
    # right edge, starts below the title bar
    grip("size_we", 2, relx=1, x=-b, y=bh, width=b, relheight=1,
         height=-(bh + c))
    # bottom edge and bottom corners
    grip("size_ns", 6, x=c, rely=1, y=-b, relwidth=1, width=-2 * c, height=b)
    grip("size_ne_sw", 7, x=0, rely=1, y=-c, width=c, height=c)
    grip("size_nw_se", 8, relx=1, x=-c, rely=1, y=-c, width=c, height=c)


def set_icon(r, path):
    """Window + taskbar icon at the exact sizes this DPI asks for."""
    try:
        u = ctypes.windll.user32
        u.LoadImageW.restype = ctypes.c_void_p
        u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_void_p, ctypes.c_void_p]
        hwnd = u.GetParent(r.winfo_id())
        dpi = u.GetDpiForWindow(hwnd) or 96
        big = u.GetSystemMetricsForDpi(11, dpi)    # SM_CXICON
        small = u.GetSystemMetricsForDpi(49, dpi)  # SM_CXSMICON
        for kind, px in ((1, big), (0, small)):
            h = u.LoadImageW(None, path, 1, px, px, 0x10)  # from file
            if h:
                u.SendMessageW(hwnd, 0x80, kind, h)        # WM_SETICON
    except Exception:
        pass


def _bar_icon(icon, px, master):
    from PIL import Image, ImageTk
    with Image.open(icon) as src:
        src.size = max(src.info["sizes"])
        img = src.convert("RGBA").resize((px, px), Image.LANCZOS)
    return ImageTk.PhotoImage(img, master=master)


def _round_corners(r, px):
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.GetParent.argtypes = [wintypes.HWND]
    u.GetParent.restype = wintypes.HWND
    hwnd = u.GetParent(r.winfo_id())
    if not hwnd:
        return

    if sys.getwindowsversion().build >= 22000:           # Windows 11
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                              ctypes.c_void_p, wintypes.DWORD]
        corner = ctypes.c_int(2)                         # DWMWCP_ROUND
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
        border = ctypes.c_uint(0x463A2A)                 # #2a3a46 as 0x00BBGGRR
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), 4)
        return

    # Windows 10: clip the window to a rounded shape
    gdi = ctypes.windll.gdi32
    gdi.CreateRoundRectRgn.restype = ctypes.c_void_p
    u.SetWindowRgn.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]

    def clip(e=None):
        if e is not None and e.widget is not r:
            return                  # <Configure> on r also fires for children
        if r.state() == "zoomed":
            u.SetWindowRgn(hwnd, None, True)
            return
        rad = px(12)
        rgn = gdi.CreateRoundRectRgn(0, 0, r.winfo_width() + 1,
                                     r.winfo_height() + 1, rad, rad)
        u.SetWindowRgn(hwnd, rgn, True)

    r.bind("<Configure>", clip, add="+")
