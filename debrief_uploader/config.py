"""Configuration: tunables, watched folders, and Windows path autodetection.

Every timing constant that can change a pairing decision lives here so it can
be logged when it does.
"""
import json
import os

APP_NAME = "DebriefUploader"
VENDOR = "GamingDiver"

SUPABASE_URL = "https://bfqgmavjnkbmqmygbfub.supabase.co"
# Publishable key: already public in the site's shipped client JS (js/auth.js:5).
# Shipping it here is not a leak. The replay decryption key is NOT here and
# never will be -- metadata comes from the replay-meta edge function.
SUPABASE_ANON_KEY = "sb_publishable_G-pB--IOnk3fa_mMvhzMmQ_SEGPsbrc"

SITE_BASE = "https://gamingdiver.com"

# ---- timing defaults (seconds unless noted) --------------------------------
DEFAULTS = {
    "pair_gap": 120,        # screenshots this close belong to the same battle
    # How long to keep a group of screenshots open in case another arrives.
    # A group that already has both tabs is treated as complete and decided
    # quickly; a lone screenshot waits longer, because its partner is the
    # thing actually worth waiting for.
    "pair_settle_full": 30,
    "pair_settle": 90,
    "max_shots": 4,         # upload at most this many screenshots per replay
    "grace": 60,            # replay may close this soon AFTER the first shot
    "max_lag": 25 * 60,     # oldest a replay may be relative to its shots
    # How long after a battle's results could appear they might still be on
    # screen. Used to decide whether an older un-scorecarded battle is a real
    # rival for a set of screenshots, or just old.
    "results_grace": 300,
    # A replay with no scorecards stops waiting for them after this long (or
    # once enough later battles have closed) and, by default, uploads without
    # them. NEW NAMES (2026-09-26): Settings.save() writes every key, so older
    # installs have orphan_timeout=2700 / upload_orphans=false on disk, and
    # reusing those names would have left them on the old behaviour forever.
    "orphan_after": 15 * 60,
    "orphan_after_n_later": 2,   # ...or this many later replays have closed
    # Upload replays that never got scorecards. The replay alone still gives
    # the full debrief (the page shows ledger damage and a Base XP estimate);
    # only the scorecard-anchored figures are missing.
    "upload_without_scorecards": True,
    "no_scorecard_min_age": 15 * 60,   # never upload one younger than this
    "settle_quiet": 2.0,    # size+mtime unchanged this long = write complete
    "settle_giveup": 60,
    "settle_sleep": 0.5,     # poll cadence while waiting for a write to finish
    "poll_interval": 2.0,   # directory scan cadence
    # A screenshots folder is a lifetime archive -- thousands of files, none
    # of which can belong to a battle that finished minutes ago. Anything
    # older than this is not even looked at.
    "shot_max_age_hours": 4,
    # Size floor, checked with a stat() before the image is ever opened.
    # A full-screen scorecard is megabytes; UI grabs and thumbnails are not.
    # NOTE: Steam's F12 writes JPEG, which can land under 1 MB at 1080p --
    # lower this to ~200 if you capture with Steam rather than Windows.
    "shot_min_kb": 1000,
    # Most new screenshots to take in one pass, so a big folder can never
    # stall a tick.
    "shot_scan_limit": 25,
    "unmatched_keep": 24 * 3600,
    # pacing -- there is NO server-side rate limit on this path and the
    # backend is a Free-tier instance that has been IO-starved before.
    "min_upload_gap": 2.0,
    "max_uploads_per_hour": 20,
    # behaviour
    "review_mode": False,   # hold every match for confirmation
    "visibility": None,     # None = inherit the account's site default
    # Training rooms are practice against bots or a friend, not a result worth
    # putting in front of the community, and they carry no scorecard to anchor
    # the Base XP research either. So they default to private whatever real
    # battles are set to. "" means "same as the setting above".
    "training_visibility": "private",
    "delete_staged_after_upload": False,
    "delete_staged_after_days": 0,   # 0 = keep forever
}

# ---- where things live -----------------------------------------------------

def app_dir():
    # Override for testing, portable installs, or running two accounts.
    env = os.environ.get("GD_UPLOADER_HOME")
    if env:
        return env
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, VENDOR, APP_NAME)
    # dev/test on other platforms
    return os.path.join(os.path.expanduser("~"), ".gamingdiver", APP_NAME.lower())


def staging_dir():
    return os.path.join(app_dir(), "staging")


def log_dir():
    return os.path.join(app_dir(), "logs")


def settings_path():
    return os.path.join(app_dir(), "settings.json")


def state_path():
    return os.path.join(app_dir(), "state.db")


# ---- Windows autodetection -------------------------------------------------

REPLAY_DIR_CANDIDATES = [
    r"C:\Program Files (x86)\Steam\steamapps\common\World of Warships Legends\replays",
    r"C:\XboxGames\World of Warships - Legends\Content\replays",
]


def _steam_root():
    """Steam install path from the registry, falling back to the usual spot."""
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return winreg.QueryValueEx(k, "SteamPath")[0].replace("/", "\\")
    except Exception:
        p = r"C:\Program Files (x86)\Steam"
        return p if os.path.isdir(p) else None


def _steam_libraries(root):
    """Every steamapps library, from libraryfolders.vdf. Best-effort text parse:
    the vdf is tiny and we only need the "path" values."""
    libs = []
    if not root:
        return libs
    libs.append(os.path.join(root, "steamapps"))
    vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    try:
        with open(vdf, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if '"path"' in line:
                    parts = line.split('"')
                    if len(parts) >= 4:
                        libs.append(os.path.join(parts[3].replace("\\\\", "\\"), "steamapps"))
    except OSError:
        pass
    out = []
    for p in libs:
        if p not in out and os.path.isdir(p):
            out.append(p)
    return out


def detect_replay_dirs():
    found = []
    root = _steam_root()
    for lib in _steam_libraries(root):
        p = os.path.join(lib, "common", "World of Warships Legends", "replays")
        if os.path.isdir(p):
            found.append(p)
    for p in REPLAY_DIR_CANDIDATES:
        if os.path.isdir(p) and p not in found:
            found.append(p)
    return found


def detect_shot_dirs():
    """Pictures\\Screenshots plus every Steam userdata screenshots folder.

    The WoWSL appid is NOT hardcoded -- we glob every game's screenshot folder
    under 760/remote. Extra folders are harmless: a screenshot that matches no
    battle is simply never uploaded.
    """
    found = []
    if os.name == "nt":
        pics = os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots")
        if os.path.isdir(pics):
            found.append(pics)
    root = _steam_root()
    if root:
        userdata = os.path.join(root, "userdata")
        try:
            for uid in os.listdir(userdata):
                remote = os.path.join(userdata, uid, "760", "remote")
                if not os.path.isdir(remote):
                    continue
                for app in os.listdir(remote):
                    p = os.path.join(remote, app, "screenshots")
                    if os.path.isdir(p):
                        found.append(p)
        except OSError:
            pass
    return found


# ---- settings --------------------------------------------------------------

class Settings(dict):
    """Defaults + on-disk overrides. Missing keys always fall back to DEFAULTS,
    so a settings file written by an older version keeps working."""

    def __init__(self, data=None):
        super().__init__(DEFAULTS)
        self["replay_dirs"] = []
        self["shot_dirs"] = []
        if data:
            self.update({k: v for k, v in data.items() if v is not None or k == "visibility"})

    @classmethod
    def load(cls):
        try:
            with open(settings_path(), "r", encoding="utf-8") as f:
                return cls(json.load(f))
        except (OSError, ValueError):
            return cls()

    def save(self):
        os.makedirs(app_dir(), exist_ok=True)
        tmp = settings_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(self), f, indent=2)
        os.replace(tmp, settings_path())

    def ensure_dirs_detected(self):
        """Fill empty folder lists by autodetection. Never overwrites a choice
        the user has made -- an empty list they set stays empty only if they
        also set the '_dirs_pinned' flag."""
        changed = False
        if not self.get("replay_dirs") and not self.get("_dirs_pinned"):
            d = detect_replay_dirs()
            if d:
                self["replay_dirs"] = d
                changed = True
        if not self.get("shot_dirs") and not self.get("_dirs_pinned"):
            d = detect_shot_dirs()
            if d:
                self["shot_dirs"] = d
                changed = True
        return changed
