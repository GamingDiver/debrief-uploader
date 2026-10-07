"""Scorecard image transform, in exact geometric parity with the browser.

The web uploader trims ultrawide black bars, crops, caps size, then encodes
JPEG q0.85
(js/replay-upload.js:1026-1050). We reproduce that arithmetic to the pixel,
because the server's OCR locates fields by NORMALISED coordinates with
knife-edge windows -- row pairing at |dy| < 0.022, ribbon badge at
0.005 < dx < 0.058, ribbon colour tile at a fixed offset box -- and those
windows were calibrated on cropped-then-capped images. Feed an uncropped frame
and every normalised coordinate shifts.

The crop also exists to preserve pixel density: "measured OCR floor is ~20px
glyph height, and a full-frame shrink of a 4K capture throws away exactly the
pixels OCR needs".

One deliberate deviation from the browser, documented in the spec: the browser
falls back to the ORIGINAL file when its re-encode comes out no smaller
(js/replay-upload.js:1049). That can ship an uncropped image -- and, for a
.webp or .heic source, an extension the server's OCR glob ignores entirely.
We always send our own transformed JPEG. Geometry parity matters more than a
few KB.
"""
import io
import math

from PIL import Image

MAX_W = 1800
QUALITY = 85

CROP_X = 0.03
CROP_Y = 0.01
CROP_W = 0.94
CROP_H = 0.98


def _jsround(x):
    """JavaScript Math.round: half rounds toward +Infinity.

    Python's round() is banker's rounding, so round(0.5)==0 and round(2.5)==2.
    Using it here would put us one pixel off the browser on every other image.
    """
    return math.floor(x + 0.5)


# Ultrawide (32:9) displays pillarbox the game between black bars. The browser
# trims them before cropping (pillarboxBounds in js/replay-upload.js), and caps
# the scale on height too: a width-only cap left a 32:9 scorecard 469 px tall
# and unreadable (replay 937246, 3 of 18 players matched). Same constants,
# same row stride, native pixels -- no resampling, so the columns agree.
BAR_MIN_ASPECT = 1.95
BAR_LUMA = 24
BAR_ROW_STRIDE = 8


def pillarbox_bounds(im):
    """Return (x0, width) of the lit picture inside black side bars."""
    w, h = im.size
    if not h or w / float(h) < BAR_MIN_ASPECT:
        return 0, w
    rgb = im.convert("RGB")
    first, last = w, -1
    for y in range(0, h, BAR_ROW_STRIDE):
        row = rgb.crop((0, y, w, y + 1)).tobytes()
        for x in range(w):
            r, g, b = row[x * 3], row[x * 3 + 1], row[x * 3 + 2]
            if 0.299 * r + 0.587 * g + 0.114 * b > BAR_LUMA:
                if x < first:
                    first = x
                if x > last:
                    last = x
    if last < 0:
        return 0, w
    x0, x1 = first, last + 1
    min_w = _jsround(h * 16 / 9.0)
    if x1 - x0 < min_w:
        x0 = max(0, min(w - min_w, _jsround((x0 + x1) / 2.0 - min_w / 2.0)))
        x1 = x0 + min_w
    if x1 - x0 > w * 0.9:
        return 0, w
    return x0, x1 - x0


def transform_geometry(w, h, max_w=MAX_W, bar_x0=0, bar_w=None):
    """Return (cx, cy, cw, ch, out_w, out_h) for a source of size w x h whose
    lit picture spans [bar_x0, bar_x0 + bar_w)."""
    bw = w if bar_w is None else bar_w
    cx = bar_x0 + _jsround(bw * CROP_X)
    cy = _jsround(h * CROP_Y)
    cw = _jsround(bw * CROP_W)
    ch = _jsround(h * CROP_H)
    if cw and ch:
        scale = min(1.0, max(max_w / float(cw), (max_w * 9 / 16.0) / ch))
    else:
        scale = 1.0
    return cx, cy, cw, ch, _jsround(cw * scale), _jsround(ch * scale)


def transform(path, max_w=MAX_W, quality=QUALITY):
    """Trim bars, crop, cap size, encode JPEG. Returns (bytes, ext, info)."""
    with Image.open(path) as im:
        im.load()
        w, h = im.size
        bx0, bw = pillarbox_bounds(im)
        cx, cy, cw, ch, ow, oh = transform_geometry(w, h, max_w, bx0, bw)
        im = im.convert("RGB")
        im = im.crop((cx, cy, cx + cw, cy + ch))
        if (ow, oh) != (cw, ch):
            im = im.resize((ow, oh), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True,
                progressive=False, subsampling=0)
    info = {"src_w": w, "src_h": h, "out_w": ow, "out_h": oh}
    return buf.getvalue(), "jpg", info


# ---- pre-flight sanity (warn, never block) ---------------------------------
# Nothing server-side ever rejects an image for being too small or the wrong
# shape; a bad capture just surfaces later as a low roster-match score that
# reads to the user as "wrong battle or unreadable?". Catching it here means
# catching it while the player still remembers the battle.

MIN_W = 1600


def inspect(path, display_aspect=None):
    """Return a list of human-readable warnings. Empty list = looks fine."""
    warns = []
    try:
        with Image.open(path) as im:
            w, h = im.size
    except Exception as e:
        return ["could not read this image (%s)" % e]

    if w < MIN_W:
        warns.append("only %dpx wide - scoreboard digits may not survive OCR "
                     "(a full-screen capture is best)" % w)
    if display_aspect and h:
        a = w / float(h)
        if abs(a - display_aspect) / display_aspect > 0.05:
            warns.append("this looks like a cropped region, not a full screen - "
                         "the last Team Result rows are often cut off")
    return warns


def looks_like_screenshot(path):
    """Cheap gate before an image is even considered, so thumbnails, icons and
    stray art in a watched folder never become upload candidates.

    Dimensions only. A file-size floor was tried and dropped: compressible
    captures (flat UI, few colours) can be genuinely small, and size tells us
    nothing that the pixel dimensions don't tell us better.
    """
    try:
        with Image.open(path) as im:
            w, h = im.size
    except Exception:
        return False
    return w >= 800 and h >= 600
