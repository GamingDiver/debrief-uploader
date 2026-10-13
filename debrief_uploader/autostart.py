"""Start when you log in to Windows: a per-user "Run" registry entry.

HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run is where Discord,
Steam and most tray apps register themselves. It needs no admin rights, it is
listed (and can be switched off) under Task Manager -> Startup apps, and it is
queryable, so the Settings checkbox reflects what is really there.

It used to be a Scheduled Task (schtasks /SC ONLOGON). Windows only lets
administrators create log-on tasks, so for a normal user the checkbox said
"ERROR: Access is denied." (tester, 2026-10-05). A task created by an older
version, or by Install-Uploader.ps1 run as admin, still counts as enabled,
and unticking removes it too.

An entry existing is not the same as the app starting (2026-10-12: it did
not start after a reboot while the box could still read as ticked). Two ways
it fails silently:
- the entry names a copy that is gone or is not the one you use (an older
  script install, a moved folder): status() calls it "stale", and repair()
  rewrites it to this copy every time the tray starts;
- Task Manager -> Startup apps switched it off, which Windows records under
  Explorer\\StartupApproved\\Run without touching the entry: status() calls it
  "blocked", and ticking the box clears that mark.
"""
import os
import shlex
import subprocess
import sys

NAME = "GamingDiver Debrief Uploader"
TASK_NAME = NAME                       # the legacy scheduled task
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def supported():
    return os.name == "nt"


def _run(args):
    """Split out so tests can drive this without touching the machine."""
    return subprocess.run(args, capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class _Registry:
    """The registry calls this needs; tests pass a dict-backed fake."""

    def get(self, name, key=RUN_KEY):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                return winreg.QueryValueEx(k, name)[0]
        except FileNotFoundError:
            return None

    def set(self, name, value):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)

    def delete(self, name, key=RUN_KEY):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except FileNotFoundError:
            pass


def _pythonw():
    """The windowed interpreter, so nothing visible starts at log-on."""
    exe = sys.executable or ""
    cand = exe.replace("python.exe", "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def command():
    """By absolute path to app.py: a Run entry has no working directory, and
    app.py puts its own folder on sys.path, so the package is found anyway.

    A built DebriefUploader.exe is the whole app. Its app.py lives in a
    PyInstaller temp folder that is deleted on exit, and the exe would read
    that path as an unknown subcommand, so the entry names the exe alone.
    installer.iss writes this same string."""
    if getattr(sys, "frozen", False):
        return '"%s" run --tray --quiet' % sys.executable
    app_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "app.py")
    return '"%s" "%s" run --tray --quiet' % (_pythonw(), app_py)


def _legacy_task(run):
    r = run(["schtasks", "/Query", "/TN", TASK_NAME])
    return r.returncode == 0


def _blocked(reg):
    """Task Manager's off switch: a binary value whose first byte is odd
    (02 = on, 03 = off; 06/07 seen on some builds)."""
    try:
        v = reg.get(NAME, key=APPROVED_KEY)
    except OSError:
        return False
    return bool(v) and isinstance(v, (bytes, bytearray)) and v[0] & 1 == 1


def _targets_exist(cmd):
    """Every quoted path in the entry (the exe, or pythonw + app.py) exists."""
    try:
        parts = shlex.split(cmd, posix=False)
    except ValueError:
        return False
    paths = [p.strip('"') for p in parts if p.startswith('"')]
    return bool(paths) and all(os.path.exists(p) for p in paths)


def status(reg=None, run=_run):
    """"on", "off", "stale" (names another or a missing copy), "blocked"
    (switched off in Task Manager), or "legacy" (an old scheduled task)."""
    if not supported():
        return "off"
    reg = reg or _Registry()
    try:
        cmd = reg.get(NAME)
    except OSError:
        cmd = None
    if cmd:
        if os.path.normcase(cmd) != os.path.normcase(command()) or not _targets_exist(cmd):
            return "stale"
        return "blocked" if _blocked(reg) else "on"
    return "legacy" if _legacy_task(run) else "off"


def is_enabled(reg=None, run=_run):
    """Only when it will really start: a stale or blocked entry reads as off,
    so the checkbox stops claiming something that will not happen."""
    return status(reg, run) in ("on", "legacy")


def enable(reg=None, run=_run):
    if not supported():
        return False, "only on Windows"
    reg = reg or _Registry()
    try:
        reg.set(NAME, command())
    except OSError as e:
        return False, "could not add the startup entry (%s)" % e
    if _blocked(reg):
        # Ticking the box is the user turning it back on.
        try:
            reg.delete(NAME, key=APPROVED_KEY)
        except OSError:
            return False, ("added the startup entry, but it is switched off in "
                           "Task Manager -> Startup apps; turn it on there")
    return True, "will start when you log in"


def disable(reg=None, run=_run):
    if not supported():
        return False, "only on Windows"
    reg = reg or _Registry()
    try:
        reg.delete(NAME)
    except OSError as e:
        return False, "could not remove the startup entry (%s)" % e
    if _legacy_task(run):
        r = run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        if r.returncode != 0:
            return False, ("removed the startup entry, but an older scheduled "
                           "task is still there; delete '%s' in Task Scheduler"
                           % TASK_NAME)
    return True, "will no longer start on its own"


def repair(reg=None, run=_run):
    """Point a stale entry at this copy. Called when the tray starts, so the
    copy you actually open is the one that starts next time. Never creates an
    entry (that is the user's choice) and never overrides Task Manager."""
    if not supported():
        return None
    reg = reg or _Registry()
    if status(reg, run) != "stale":
        return None
    try:
        reg.set(NAME, command())
    except OSError as e:
        return "could not update the startup entry (%s)" % e
    return "startup entry now points at this copy"


def explain(reg=None, run=_run):
    """One line for Settings when the box is unticked for a reason."""
    s = status(reg, run)
    if s == "blocked":
        return ("Switched off in Task Manager -> Startup apps. Tick the box to "
                "turn it back on.")
    if s == "stale":
        return ("The startup entry points at another or a removed copy. Tick the "
                "box to point it at this one.")
    return ""
