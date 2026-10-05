"""Start when Windows starts, as a per-user Scheduled Task.

schtasks.exe rather than PowerShell: no execution policy to argue with, no
admin rights, and it is queryable so the tray can show a checkbox that
reflects reality instead of a preference we stored and hoped about.
"""
import os
import subprocess
import sys

TASK_NAME = "GamingDiver Debrief Uploader"


def _run(args):
    """Split out so tests can drive this without touching the machine."""
    return subprocess.run(args, capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def supported():
    return os.name == "nt"


def _pythonw():
    """The windowed interpreter, so the task starts nothing visible."""
    exe = sys.executable or ""
    cand = exe.replace("python.exe", "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def _command():
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return '"%s" -m debrief_uploader run --tray --quiet' % _pythonw(), app_dir


def is_enabled(run=_run):
    if not supported():
        return False
    r = run(["schtasks", "/Query", "/TN", TASK_NAME])
    return r.returncode == 0


def enable(run=_run):
    if not supported():
        return False, "only on Windows"
    cmd, cwd = _command()
    # /F overwrites an existing task, so this is also the repair path.
    r = run(["schtasks", "/Create", "/SC", "ONLOGON", "/TN", TASK_NAME,
             "/TR", cmd, "/F"])
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip() or "schtasks refused"
    return True, "will start when you log in"


def disable(run=_run):
    if not supported():
        return False, "only on Windows"
    r = run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip() or "schtasks refused"
    return True, "will no longer start on its own"
