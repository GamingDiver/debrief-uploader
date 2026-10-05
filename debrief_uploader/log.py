"""Rolling log. File paths and battle metadata only -- never tokens, never
image contents, never key material."""
import os
import sys
import time

KEEP_DAYS = 7


class Log:
    def __init__(self, directory=None, echo=True):
        self.dir = directory
        # pythonw.exe -- which is how the tray runs, with no console -- sets
        # sys.stdout to None. Writing to it then raises AttributeError from
        # inside the logger, which is a spectacularly unhelpful place to die.
        self.echo = bool(echo) and sys.stdout is not None
        self.lines = []            # in-memory tail for the UI
        if directory:
            os.makedirs(directory, exist_ok=True)
            self._prune()

    def _path(self):
        return os.path.join(self.dir, "uploader-%s.log" % time.strftime("%Y-%m-%d"))

    def _prune(self):
        cutoff = time.time() - KEEP_DAYS * 86400
        try:
            for n in os.listdir(self.dir):
                p = os.path.join(self.dir, n)
                if n.startswith("uploader-") and os.path.getmtime(p) < cutoff:
                    os.remove(p)
        except OSError:
            pass

    def _write(self, level, msg):
        line = "%s %-5s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, msg)
        self.lines.append(line)
        del self.lines[:-400]
        if self.echo:
            try:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except Exception:
                self.echo = False        # console went away; keep the file
        if self.dir:
            try:
                with open(self._path(), "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def info(self, m):
        self._write("info", m)

    def warn(self, m):
        self._write("warn", m)

    def error(self, m):
        self._write("error", m)
