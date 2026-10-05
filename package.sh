#!/usr/bin/env bash
# Build the distributable zip -- but only if the tests pass.
#
# v1.0.8 shipped with an empty __init__.py and died on startup. The edit that
# broke it was made AFTER the test run, and the zip was built by hand from
# whatever was on disk. A build that cannot fail is how that reaches a user.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
OUT="${1:-$PWD/DebriefUploader.zip}"

echo "== tests =="
python3 -m unittest discover -s tests -q

# The tkinter windows only build where Tk exists, and the default interpreter
# here has none. Run them with one that does -- GUI code is exactly where the
# faults in this project have been, precisely because nothing executed it.
for TKPY in python3 /usr/bin/python3 python3.13 python3.12; do
  if command -v "$TKPY" >/dev/null 2>&1 && "$TKPY" -c "import tkinter" 2>/dev/null; then
    echo "== window tests ($TKPY) =="
    "$TKPY" -m unittest tests.test_ui_builds -q
    break
  fi
done

echo "== sanity =="
VERSION="$(python3 -c 'import debrief_uploader; print(debrief_uploader.__version__)')"
[ -n "$VERSION" ] || { echo "no version"; exit 1; }
python3 -m debrief_uploader --help >/dev/null
echo "version $VERSION, CLI starts"

echo "== packaging =="
find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
rm -f "$OUT"
( cd .. && zip -qr "$OUT" debrief-uploader \
    -x '*__pycache__*' '*.pyc' '*.DS_Store' '*/.tmp-home*' '*/DebriefUploader.zip' )

echo "== verifying the zip actually runs =="
TMP="$(mktemp -d)"
unzip -q "$OUT" -d "$TMP"
( cd "$TMP/debrief-uploader" \
  && GD_UPLOADER_HOME="$TMP/home" python3 -m debrief_uploader doctor >/dev/null )
rm -rf "$TMP"

echo
echo "OK  $OUT  ($(du -h "$OUT" | cut -f1), v$VERSION)"
