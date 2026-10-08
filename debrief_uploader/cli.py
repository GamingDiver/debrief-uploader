"""Command line: setup, login, run, status, review, logout.

The CLI is the whole app. The tray is a thin layer on top of the same engine,
so anything that can be done from the tray can be done here -- which is what
makes the thing debuggable on someone else's PC over a chat window.
"""
import argparse
import getpass
import os
import sys
import time

from . import api, config, matcher, oauth, single_instance
from .api import ApiError, Client, load_session
from .engine import Engine
from .log import Log
from .state import Store

STATE_LABEL = {
    matcher.NEW: "reading",
    matcher.AWAITING_SHOTS: "waiting for scorecards",
    matcher.HELD: "needs you",
    matcher.READY: "queued",
    matcher.UPLOADING: "uploading",
    matcher.UPLOADED: "uploaded",
    matcher.ORPHAN: "no scorecards",
    matcher.FAILED: "failed",
    matcher.SKIPPED: "skipped",
}


def _boot(echo=True):
    s = config.Settings.load()
    if s.ensure_dirs_detected():
        s.save()
    store = Store(config.state_path())
    client = Client(load_session())
    log = Log(config.log_dir(), echo=echo)
    return s, store, client, log


def _label(row):
    ship = (row["player_ship"] or "").split("-", 1)[-1].replace("-", " ")
    when = time.strftime("%H:%M", time.localtime(row["t_close"] or 0))
    bits = [b for b in (ship or None, row["map_name"] or None) if b]
    return "%s  %s" % (when, " / ".join(bits) or (row["filename"] or "")[:40])


# ---- commands --------------------------------------------------------------

def cmd_setup(args):
    s = config.Settings.load()
    s.ensure_dirs_detected()
    if args.replay_dir:
        s["replay_dirs"] = args.replay_dir
        s["_dirs_pinned"] = True
    if args.shot_dir:
        s["shot_dirs"] = args.shot_dir
        s["_dirs_pinned"] = True
    if args.exclude_dir:
        s["exclude_dirs"] = config.dedupe_dirs(
            s["exclude_dirs"] + [os.path.normpath(d) for d in args.exclude_dir])
    s.save()
    print("Replay folders:")
    for d in s["replay_dirs"] or ["  (none found -- pass --replay-dir)"]:
        print("  %s" % d)
    print("Screenshot folders:")
    for d in s["shot_dirs"] or ["  (none found -- pass --shot-dir)"]:
        print("  %s" % d)
    print("Excluded (never read):")
    for d in s["exclude_dirs"] or ["(none)"]:
        print("  %s" % d)
    print("\nState and staged replays: %s" % config.app_dir())
    if not s["replay_dirs"] or not s["shot_dirs"]:
        return 1
    if args.start:
        first = not s.get("watching_since")
        s.confirm_watching()
        print("\nWatching %s. Only battles and screenshots from now on are "
              "considered." % ("starts now" if first else "was already on"))
    elif not s.get("watching_confirmed"):
        print("\nNot watching yet. If these folders are right (exclude "
              "anything you must not upload with --exclude-dir), run:\n"
              "  Debrief.cmd setup --start")
    return 0


def cmd_login(args):
    s, store, client, log = _boot(echo=False)
    try:
        if args.email:
            email = args.email if isinstance(args.email, str) else input("Email: ")
            pw = os.environ.get("GD_PASSWORD") or getpass.getpass("Password: ")
            client.sign_in_password(email, pw)
        else:
            oauth.sign_in_browser(client, args.provider)
    except ApiError as e:
        print("Sign-in failed: %s" % e.message)
        return 1
    print("Signed in as %s" % (client.session.email or client.session.user_id))
    return 0


def cmd_logout(args):
    s, store, client, log = _boot(echo=False)
    client.sign_out()
    print("Signed out.")
    return 0


def cmd_status(args):
    s, store, client, log = _boot(echo=False)
    sess = client.session
    print("Account : %s" % (sess.email or sess.user_id or "not signed in"))
    print("Watching: %d replay folder(s), %d screenshot folder(s)"
          % (len(s["replay_dirs"]), len(s["shot_dirs"])))
    skew = store.get("clock_skew_hours")
    if skew:
        print("Note    : this PC's game clock differs from its file clock by "
              "about %s h. Pairing is unaffected (it uses file times)." % skew)
    rows = store.recent(args.limit)
    if not rows:
        print("\nNothing seen yet.")
        return 0
    print("")
    for r in rows:
        line = "  %-22s %-22s" % (STATE_LABEL.get(r["state"], r["state"]), _label(r))
        if r["short_code"]:
            line += "  %s/wowslegends/replays/?r=%s" % (config.SITE_BASE, r["short_code"])
        elif r["note"]:
            line += "  %s" % r["note"]
        elif r["last_error"]:
            line += "  %s" % r["last_error"]
        print(line)
    return 0


def cmd_review(args):
    """Resolve everything the engine refused to guess at."""
    s, store, client, log = _boot(echo=False)
    held = [r for r in store.replays([matcher.HELD])]
    orphans = [r for r in store.replays([matcher.ORPHAN])]
    if not held and not orphans:
        print("Nothing needs you.")
        return 0

    for r in held:
        shots = store.shots_for(r["md5"])
        print("\n%s" % _label(r))
        print("  %s" % (r["note"] or "needs confirmation"))
        for sh in shots:
            print("    %s" % os.path.basename(sh["path"]))
        others = [o for o in store.replays([matcher.AWAITING_SHOTS])
                  if o["md5"] != r["md5"]]
        print("  [u] upload as-is   [s] skip   %s  [q] quit"
              % ("[1-%d] give the screenshots to another battle" % len(others)
                 if others else ""))
        for i, o in enumerate(others, 1):
            print("      %d) %s" % (i, _label(o)))
        choice = input("  > ").strip().lower()
        if choice == "q":
            break
        if choice == "u":
            Engine(s, store, client, log).confirm(r["md5"])
            print("  queued.")
        elif choice == "s":
            Engine(s, store, client, log).skip(r["md5"])
            print("  skipped.")
        elif choice.isdigit() and others and 1 <= int(choice) <= len(others):
            Engine(s, store, client, log).reassign(r["md5"], others[int(choice) - 1]["md5"])
            print("  moved.")

    if orphans and not args.no_orphans:
        if s.get("upload_without_scorecards"):
            print("\n%d battle(s) never got scorecards; they upload without them." % len(orphans))
        else:
            print("\n%d battle(s) never got scorecards. They are kept in %s and are "
                  "not uploaded." % (len(orphans), config.staging_dir()))
    return 0


def tray_check():
    """Can the tray start? Imports what it needs and finds its icon.

    A PyInstaller build that misses a hidden import or the resources folder
    only fails when the tray starts, and the installer exe has no console to
    show why, so release CI runs `doctor` on the built exe and reads this
    through the exit code."""
    try:
        import tkinter  # noqa: F401
        from PIL import Image  # noqa: F401
        import pystray  # noqa: F401
        from .ui import _res
    except Exception as e:      # any import failure means no tray
        return False, "cannot load %s" % e
    ico = _res("app.ico")
    if not os.path.isfile(ico):
        return False, "icon missing at %s" % ico
    return True, "ok"


def cmd_doctor(args):
    """One command that answers 'why is nothing happening?'."""
    s, store, client, log = _boot(echo=False)
    eng = Engine(s, store, client, log)
    print("Debrief Uploader %s   Python %s   %s"
          % (__import__("debrief_uploader").__version__,
             sys.version.split()[0], config.app_dir()))
    tray_ok, why = tray_check()
    print("Tray: %s" % why)
    print("")
    for line in eng.diagnose():
        print(line)
    print("")
    logs = sorted(os.listdir(config.log_dir())) if os.path.isdir(config.log_dir()) else []
    if logs:
        print("LAST LOG LINES (%s)" % logs[-1])
        try:
            with open(os.path.join(config.log_dir(), logs[-1]), "r",
                      encoding="utf-8", errors="replace") as f:
                for line in f.readlines()[-12:]:
                    print("  " + line.rstrip())
        except OSError:
            pass
    # Only Windows ships the tray; elsewhere a missing display is expected.
    return 1 if (os.name == "nt" and not tray_ok) else 0


def _preflight(s, client, log, fatal=True):
    """Everything the user needs to know before the first tick.

    This used to sit AFTER the `--tray` branch returned, so the tray -- the
    way almost everyone actually starts the app -- silently skipped all of it:
    no folder counts, no sign-in warning, no missing-folder error. That is
    precisely the information needed when nothing appears to happen.
    Returns False if we cannot usefully run at all.
    """
    log.info("Debrief Uploader %s starting"
             % __import__("debrief_uploader").__version__)
    if not s["replay_dirs"]:
        log.error("no replay folder is being watched" + ("" if fatal else
                  " - open Settings from the tray icon to choose one"))
        if fatal:
            return False
    if not s.get("watching_confirmed"):
        log.error("not watching yet: nothing is read until you check the "
                  "folders - " + ("open Settings from the tray icon and press "
                                  "Start watching" if not fatal else
                                  "run 'Debrief.cmd setup', then "
                                  "'Debrief.cmd setup --start'"))
    log.info("watching %d replay folder(s) and %d screenshot folder(s)%s"
             % (len(s["replay_dirs"]), len(s["shot_dirs"]),
                ", excluding %d" % len(s["exclude_dirs"])
                if s["exclude_dirs"] else ""))
    for d in s["replay_dirs"] + s["shot_dirs"]:
        if not os.path.isdir(d):
            log.warn("folder is missing: %s" % d)
    if not s["shot_dirs"]:
        log.error("no screenshot folder is being watched, so scorecards will "
                  "never be found. Run 'Debrief.cmd setup --shot-dir "
                  "\"%%USERPROFILE%%\\Pictures\\Screenshots\"'")
    if not client.session.signed_in:
        log.error("not signed in: battles cannot be identified and nothing "
                  "will upload. Run 'Debrief.cmd login --email'")
    else:
        log.info("signed in as %s" % (client.session.email
                                      or client.session.user_id))
    return True


def cmd_run(args):
    s, store, client, log = _boot(echo=not args.quiet)

    # One watcher per machine. Two would both upload the same battle, and
    # double-clicking the launcher again because you are not sure it worked is
    # a completely normal thing to do.
    try:
        lock = single_instance.Lock().acquire()
    except single_instance.AlreadyRunning:
        log.info("already running - look for the icon in your system tray")
        return 0

    try:
        # The tray can fix its own configuration now, so a missing folder is
        # something to report inside it, not a reason to refuse to start.
        if not _preflight(s, client, log, fatal=not args.tray):
            return 1

        if args.tray:
            from . import tray
            return tray.run(s, store, client, log)

        eng = Engine(s, store, client, log)
        try:
            while True:
                try:
                    eng.tick()
                except ApiError as e:
                    log.error(e.message)
                    if e.terminal:
                        eng.blocked = e.message
                except Exception as e:          # never die on one bad tick
                    log.error("unexpected: %s" % e)
                if args.once:
                    break
                time.sleep(s["poll_interval"])
        except KeyboardInterrupt:
            log.info("stopped")
        return 0
    finally:
        lock.release()


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="debrief-uploader",
        description="Watch for World of Warships Legends replays and their "
                    "post-battle scorecards, and upload matched sets to "
                    "GamingDiver Debrief.")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("setup", help="find (or set) the folders to watch")
    q.add_argument("--replay-dir", action="append")
    q.add_argument("--shot-dir", action="append")
    q.add_argument("--exclude-dir", action="append",
                   help="never read anything inside this folder")
    q.add_argument("--start", action="store_true",
                   help="the folders are right: start watching from now")
    q.set_defaults(fn=cmd_setup)

    q = sub.add_parser("login", help="sign in")
    q.add_argument("--email", nargs="?", const=True,
                   help="use email + password instead of the browser")
    q.add_argument("--provider", default="discord", choices=oauth.PROVIDERS)
    q.set_defaults(fn=cmd_login)

    q = sub.add_parser("logout", help="sign out and forget the tokens")
    q.set_defaults(fn=cmd_logout)

    q = sub.add_parser("run", help="watch and upload")
    q.add_argument("--tray", action="store_true", help="show a system tray icon")
    q.add_argument("--once", action="store_true", help="one pass, then exit")
    q.add_argument("--quiet", action="store_true")
    q.set_defaults(fn=cmd_run)

    q = sub.add_parser("status", help="what has been seen and uploaded")
    q.add_argument("--limit", type=int, default=15)
    q.set_defaults(fn=cmd_status)

    q = sub.add_parser("doctor", help="explain why nothing is happening")
    q.set_defaults(fn=cmd_doctor)

    q = sub.add_parser("review", help="resolve anything that needs a decision")
    q.add_argument("--no-orphans", action="store_true")
    q.set_defaults(fn=cmd_review)

    args = p.parse_args(argv)
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        return 130
