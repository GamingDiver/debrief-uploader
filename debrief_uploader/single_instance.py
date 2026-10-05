"""One tray icon, not four.

The launcher is a file people double-click, and they double-click it again
when they are not sure it worked. Two copies watching the same folders would
both try to upload the same battle.

NOT a listening socket. That was the first implementation, and binding one
makes the OS ask "allow Python to accept incoming connections?" -- a macOS
local-network prompt, a Windows Defender Firewall prompt. A background tray
app has no business triggering a firewall dialog, and a user who clicks Block
would be left with a lock that silently never works.

So: a named kernel mutex on Windows, an exclusive file lock elsewhere. Both
are released by the OS when the process dies, however it dies, so there is no
stale lock to clean up after a crash or a hard reboot.
"""
import os

from . import config

NAME = "GamingDiver.DebriefUploader"


class AlreadyRunning(Exception):
    pass


class Lock:
    def __init__(self, name=NAME):
        self.name = name
        self._handle = None
        self._fh = None

    # ---- windows: named mutex --------------------------------------------
    def _acquire_windows(self):
        import ctypes
        from ctypes import wintypes
        ERROR_ALREADY_EXISTS = 183
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = wintypes.HANDLE
        # "Local\\" scopes it to the logon session, which is the right scope:
        # two users on one PC each get their own uploader.
        h = k32.CreateMutexW(None, True, "Local\\" + self.name)
        if not h:
            return                      # cannot tell; better to run than not
        if k32.GetLastError() == ERROR_ALREADY_EXISTS:
            k32.CloseHandle(h)
            raise AlreadyRunning("another copy is already running")
        self._handle = h

    # ---- everywhere else: exclusive file lock ----------------------------
    def _acquire_posix(self):
        import fcntl
        os.makedirs(config.app_dir(), exist_ok=True)
        path = os.path.join(config.app_dir(), "%s.lock" % self.name)
        fh = open(path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise AlreadyRunning("another copy is already running")
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def acquire(self):
        if os.name == "nt":
            self._acquire_windows()
        else:
            self._acquire_posix()
        return self

    def release(self):
        if self._handle is not None:
            import ctypes
            ctypes.windll.kernel32.ReleaseMutex(self._handle)
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False
