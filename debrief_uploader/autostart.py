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
"""
import os
import subprocess
import sys

NAME = "GamingDiver Debrief Uploader"
TASK_NAME = NAME                       # the legacy scheduled task
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def supported():
    return os.name == "nt"


def _run(args):
    """Split out so tests can drive this without touching the machine."""
    return subprocess.run(args, capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class _Registry:
    """The three registry calls this needs; tests pass a dict-backed fake."""

    def get(self, name):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                return winreg.QueryValueEx(k, name)[0]
        except FileNotFoundError:
            return None

    def set(self, name, value):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)

    def delete(self, name):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
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
    app.py puts its own folder on sys.path, so the package is found anyway."""
    app_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "app.py")
    return '"%s" "%s" run --tray --quiet' % (_pythonw(), app_py)


def _legacy_task(run):
    r = run(["schtasks", "/Query", "/TN", TASK_NAME])
    return r.returncode == 0


def is_enabled(reg=None, run=_run):
    if not supported():
        return False
    reg = reg or _Registry()
    try:
        if reg.get(NAME):
            return True
    except OSError:
        pass
    return _legacy_task(run)


def enable(reg=None, run=_run):
    if not supported():
        return False, "only on Windows"
    reg = reg or _Registry()
    try:
        reg.set(NAME, command())
    except OSError as e:
        return False, "could not add the startup entry (%s)" % e
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
