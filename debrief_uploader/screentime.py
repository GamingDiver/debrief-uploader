"""The battle time printed on a result screen, and which battle it names.

Every WoWS Legends result screen (Personal, Team Result, and the same screens
reopened later from port) prints the mode/map line and, under it, the
battle's date and minute in the top-left corner:

    STANDARD - DOMINATION - TRAP
    09.11.2026 21:43

Measured on 1,837 screenshots whose battle was known for certain, that minute
sits 3 to 148 seconds BEFORE the replay header's dateTime (the screen shows
the battle's start, truncated to the minute; the header is when the client
began recording, a little later). A gap of minutes means another battle's
screen. A gap of hours or days means the PC clock is off (the header follows
the PC clock, the screen does not), so it says nothing either way.

This matters when screenshots are taken long after the battle -- reopening
old results from port, say. File times then say nothing about which battle a
screenshot is of, and two battles' tabs taken seconds apart look like one
group. The screen time tells them apart.

Pure logic: no OCR, no clock, no I/O. Reading the time off an image is
screenocr.py's job.
"""
import re
import unicodedata
from datetime import datetime
from statistics import median

# screen minute minus header dateTime, seconds. Observed [-148, -3]; the
# margin keeps a slow-loading client inside.
WINDOW = (-180.0, 30.0)
TYPICAL = -60.0                  # the median gap on a correct clock
LATE_JOIN_MAX = 1200.0 + 180.0   # a battle's time limit, plus the window
CONTRADICT_MAX = 12 * 3600.0     # beyond this a gap is the clock, not a battle
ZONE_STEP = 1800.0               # time zones differ by whole half hours

# Two screenshots whose screen times differ by more than this are of
# different battles. Tabs of one battle print the same minute; the margin
# only absorbs a misread digit's neighbour. Two battles cannot start this
# close together.
SAME_BATTLE_S = 120.0

MATCH = "match"
LATE_JOIN = "late_join"          # same battle if the recording began late
CONTRADICTS = "contradicts"
CLOCK_OFFSET = "clock_offset"

# "09.11.2026 21:43", with OCR's habits: a comma for a dot ("28.10,2026"),
# junk glyphs after the minute ("18:30l"), a dot read for the space
# ("18.07.2026.00:52"). Year-first is accepted too, for a
# client that prints it that way; a slash date is not, because D/M and M/D
# cannot be told apart and a wrong guess is worse than no reading.
_DMY = re.compile(r"(?<!\d)(\d{1,2})\s?[.,]\s?(\d{1,2})\s?[.,]\s?(\d{4})(?:\s+|\s?[.,]\s?)(\d{1,2})\s?[:.]\s?(\d{2})(?!\d)")
_YMD = re.compile(r"(?<!\d)(\d{4})\s?[.\-/年]\s?(\d{1,2})\s?[.\-/月]\s?(\d{1,2})日?\s+(\d{1,2})\s?[:.]\s?(\d{2})(?!\d)")
# the mode/map line: "STANDARD - DOMINATION - TRAP" (em dashes on screen)
_MODE_SEP = re.compile(r"\s+[-–—]\s+")


def _mk(y, mo, d, h, mi):
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi))
    except ValueError:
        return None
    return dt if 2020 <= dt.year <= 2100 else None


def parse_screen_time(text):
    """The battle time in one line of screen text, or None."""
    s = str(text or "")
    m = _DMY.search(s)
    if m:
        return _mk(m.group(3), m.group(2), m.group(1), m.group(4), m.group(5))
    m = _YMD.search(s)
    if m:
        return _mk(m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
    return None


def find_screen_header(lines):
    """(battle time, mode/map line) from a screen's text lines, top down.
    The mode line is the last dash-separated line above the time."""
    mode = None
    for t in lines:
        dt = parse_screen_time(t)
        if dt is not None:
            return dt, mode
        if len(_MODE_SEP.split(str(t))) >= 2:
            mode = str(t)
    return None, None


def _key(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def names_map(mode_line, map_name):
    """Does the screen's mode/map line end in this replay's map? False when
    either is unknown or the names differ in form (a localised client, or an
    internal map id): it can only ever confirm."""
    if not mode_line or not map_name:
        return False
    return _key(_MODE_SEP.split(mode_line)[-1]) == _key(map_name) != ""


def parse_header_time(value):
    """A replay's start as a naive local datetime.

    Accepts the header's "DD.MM.YYYY HH:MM:SS" and the app's played_at, which
    is that same wall clock written as ISO with a 'Z' it never had
    (api.iso_utc). Both sides stay naive: the screen and the header are local
    wall clocks, so no zone conversion may touch either."""
    if not value:
        return None
    s = str(value).strip()
    try:
        return datetime.strptime(s[:19], "%d.%m.%Y %H:%M:%S")
    except ValueError:
        pass
    try:
        return datetime.strptime(s.replace("Z", "").replace("T", " ")[:19],
                                 "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def relation(screen, header, offset_s=0.0, joined_late=False):
    """How a screen's battle time relates to one replay's start.

    offset_s     a known PC-clock error (machine_offset); 0 when unknown
    joined_late  the recording may have begun after the battle did (the
                 screen's map line names this replay's map)

    MATCH         same battle
    LATE_JOIN     the screen is up to one battle-length EARLIER and
                  joined_late allows it: same battle, recording began late
    CONTRADICTS   minutes apart: another battle's screen
    CLOCK_OFFSET  hours/days apart, or a whole-half-hour zone shift: the PC
                  clock is off, so this says nothing either way
    None          one side unreadable
    """
    if screen is None or header is None:
        return None
    d = (screen - header).total_seconds() - offset_s
    lo, hi = WINDOW
    if lo <= d <= hi:
        return MATCH
    if abs(d) > CONTRADICT_MAX:
        return CLOCK_OFFSET
    if abs(d) >= ZONE_STEP + lo:
        r = d - round(d / ZONE_STEP) * ZONE_STEP
        if lo <= r <= hi:
            return CLOCK_OFFSET
    if joined_late and -LATE_JOIN_MAX <= d < lo:
        return LATE_JOIN
    return CONTRADICTS


def machine_offset(pairs, min_pairs=3, spread_s=300.0):
    """This PC's clock error from (screen, header) pairs it already matched.

    0 when the clock is right (the median gap is already inside the window),
    when there are too few pairs, or when they disagree (a stray wrong pair
    must not move it). Otherwise the median gap minus the typical one."""
    gaps = [(s - h).total_seconds() for s, h in pairs if s and h]
    if len(gaps) < min_pairs:
        return 0.0
    mid = median(gaps)
    if max(abs(g - mid) for g in gaps) > spread_s:
        return 0.0
    if WINDOW[0] <= mid <= WINDOW[1]:
        return 0.0
    return mid - TYPICAL


def pick_battle(screen, battles, offset_s=0.0, mode_line=None):
    """The one battle a screen's time names, or None.

    battles: iterable of (key, header datetime, map name or None). A battle
    within the window wins; failing that, one the screen fits only as a late
    join (map line naming its map). None when the time is unreadable, when
    nothing fits, or when two candidates fit equally (never guess)."""
    if screen is None:
        return None
    battles = list(battles)
    for want, late in ((MATCH, False), (LATE_JOIN, True)):
        hits = [b[0] for b in battles
                if relation(screen, b[1], offset_s,
                            joined_late=late and names_map(mode_line, b[2])) == want]
        if len(hits) == 1:
            return hits[0]
        if hits:
            return None
    return None


def split_by_screen_time(shots):
    """Split one burst of screenshots wherever the screen time changes.

    shots: [(shot, screen datetime or None)] in capture order. Reopening old
    results from port gives bursts like 22:03-22:04 holding two battles' tabs
    (20:27, 20:27, 20:49, 20:49) that the capture-time gap alone joins. A
    shot whose time could not be read stays with its neighbour.
    Returns [[shot, ...], ...]."""
    groups = []
    cur_t = None
    for shot, t in shots:
        same = (t is None or cur_t is None
                or abs((t - cur_t).total_seconds()) <= SAME_BATTLE_S)
        if groups and same:
            groups[-1].append(shot)
            cur_t = cur_t or t
        else:
            groups.append([shot])
            cur_t = t
    return groups


# ---- storage form -----------------------------------------------------------
# The shots table keeps the time as text: NULL = not read yet, "" = read and
# not found, else "YYYY-MM-DD HH:MM".

def to_text(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def from_text(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M")
    except ValueError:
        return None
