"""Entry point for the packaged executable and the .cmd launchers.

Double-clicked with no arguments it starts watching with a tray icon; run from
a terminal it behaves exactly like `python -m debrief_uploader <command>`.
"""
import sys

from debrief_uploader.cli import main

if __name__ == "__main__":
    argv = sys.argv[1:] or ["run", "--tray"]
    sys.exit(main(argv))
