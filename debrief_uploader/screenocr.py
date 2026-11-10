"""Read the battle time off a result screenshot, on this PC.

Uses Windows' own OCR engine (Windows.Media.Ocr, part of Windows 10 and 11),
reached through the PowerShell that ships with Windows. That adds no package
to the app and sends nothing anywhere: the image never leaves the PC, and the
only thing kept is the parsed time and the mode/map line above it.

Only a small corner is read: the top-left band where the game prints
"STANDARD - DOMINATION - TRAP" / "09.11.2026 21:43", cropped and enlarged
first because that line is about 12 px tall on a 1080p screen.

Everywhere this cannot work (not Windows, no OCR language installed,
PowerShell blocked, a timeout) it returns nothing, and the app matches by
file time exactly as it always has.
"""
import base64
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageOps

from . import images, screentime

# The corner holding the time, as fractions of the game picture (after any
# ultrawide bars are trimmed). Measured on 381 result screens: the date
# line ends by x = 0.154 and its centre lies between y = 0.079 and 0.14 (the
# lower ones are the same screens reopened from port, under the port menu).
# The band is kept that tight because Windows OCR missed the line at about
# 30 px glyph height and read it at 40 px: the narrower the band, the more
# it can be enlarged within the engine's size limit.
BAND_W = 0.3
BAND_H = 0.22
# Windows.Media.Ocr refuses images larger than OcrEngine.MaxImageDimension
# (2600 px) on either side.
MAX_DIM = 2600
MAX_SCALE = 5.0
TIMEOUT_S = 90

_unavailable = None     # why OCR cannot run on this PC, once known
_timeouts = 0
MAX_TIMEOUTS = 2        # then stop trying for this session: each one blocks


def available():
    return sys.platform == "win32" and _unavailable is None


def why_unavailable():
    if sys.platform != "win32":
        return "not Windows"
    return _unavailable


def header_crop(path):
    """The enlarged greyscale top-left band of a screenshot, as a PIL image."""
    with Image.open(path) as im:
        im.load()
        x0, pw = images.pillarbox_bounds(im)
        w, h = im.size
        band = im.crop((x0, 0, x0 + max(1, int(pw * BAND_W)), max(1, int(h * BAND_H))))
    g = ImageOps.autocontrast(band.convert("L"))
    bw, bh = g.size
    scale = min(MAX_SCALE, MAX_DIM / float(max(bw, bh)))
    if scale > 1.0:
        g = g.resize((max(1, int(bw * scale)), max(1, int(bh * scale))),
                     Image.LANCZOS)
    return g


# Reads every image listed in DEBRIEF_OCR_PATHS ('|'-separated; no Windows
# path can contain '|') and prints "<index><TAB><line text>" per text line,
# top to bottom. "status<TAB>..." lines report setup problems.
_PS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
  $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
  $null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
  $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
} catch { "status`tnowinrt`t" + $_.Exception.Message; exit 0 }
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$t) {
  $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op))
  $null = $task.Wait(-1)
  $task.Result
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($engine -eq $null) {
  foreach ($lang in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    if ($engine -ne $null) { break }
  }
}
if ($engine -eq $null) { "status`tnoengine"; exit 0 }
"status`tengine`t" + $engine.RecognizerLanguage.LanguageTag + "`t" + [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
$i = 0
foreach ($p in $env:DEBRIEF_OCR_PATHS.Split('|')) {
  try {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($p)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
      $dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
      $bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
      $res = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
      foreach ($l in $res.Lines) { "$i`t" + $l.Text }
    } finally { $stream.Dispose() }
  } catch { "status`terror`t$i`t" + $_.Exception.Message }
  $i++
}
"""


def _powershell():
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    p = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return p if os.path.exists(p) else "powershell.exe"


def ocr_lines(image_paths, timeout=TIMEOUT_S, log=None):
    """Windows OCR text lines for each image: {index: [line, ...]}.

    Returns None when OCR cannot run here (and remembers why, so a PC
    without it is not asked again every pass)."""
    global _unavailable, _timeouts
    if not available() or not image_paths:
        return None
    script = base64.b64encode(_PS.encode("utf-16-le")).decode("ascii")
    env = dict(os.environ, DEBRIEF_OCR_PATHS="|".join(image_paths))
    try:
        out = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand", script],
            env=env, capture_output=True, timeout=timeout,
            # no console window flashing up from the tray app
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        _timeouts += 1
        if _timeouts >= MAX_TIMEOUTS:
            _unavailable = "Windows OCR keeps timing out"
        if log:
            log.warn("reading the battle time timed out; matching by file time")
        return None
    except OSError as e:
        _unavailable = "PowerShell could not start (%s)" % e
        if log:
            log.warn("cannot read battle times from screenshots: %s" % _unavailable)
        return None
    text = out.stdout.decode("utf-8", "replace")
    lines = {}
    for raw in text.splitlines():
        parts = raw.rstrip("\r").split("\t")
        if parts[0] == "status":
            kind = parts[1] if len(parts) > 1 else ""
            if kind in ("noengine", "nowinrt"):
                _unavailable = ("no Windows OCR language is installed"
                                if kind == "noengine" else
                                "Windows OCR is not available (%s)"
                                % "\t".join(parts[2:]))
                if log:
                    log.warn("cannot read battle times from screenshots: %s - "
                             "matching by file time" % _unavailable)
                return None
            if kind == "error" and log:
                log.warn("could not read the battle time from one screenshot: %s"
                         % "\t".join(parts[3:]))
            continue
        try:
            i = int(parts[0])
        except ValueError:
            continue
        lines.setdefault(i, []).append("\t".join(parts[1:]))
    if out.returncode and not lines and log:
        log.warn("reading battle times failed (PowerShell exit %d): %s"
                 % (out.returncode, out.stderr.decode("utf-8", "replace")[:300]))
    return lines


def read_headers(paths, log=None):
    """{path: (battle time or None, mode/map line or None)} for each
    screenshot this could read. A path missing from the result was not
    tried (OCR unavailable); (None, None) means it was read and the time was
    not found."""
    if not available() or not paths:
        return {}
    tmp = tempfile.mkdtemp(prefix="debrief-ocr-")
    try:
        crops = []                 # (path, crop file, inverted?)
        for n, p in enumerate(paths):
            try:
                g = header_crop(p)
            except Exception as e:
                if log:
                    log.warn("could not open %s to read its battle time (%s)"
                             % (os.path.basename(p), e))
                continue
            # The game prints light text on a dark sea; OCR engines are tuned
            # for dark on light. Both are read, the plain one is preferred.
            for inv, img in ((False, g), (True, ImageOps.invert(g))):
                f = os.path.join(tmp, "%d%s.png" % (n, "i" if inv else ""))
                img.save(f)
                crops.append((p, f))
        if not crops:
            return {}
        lines = ocr_lines([f for _p, f in crops], log=log)
        if lines is None:
            return {}
        out = {}
        for i, (p, _f) in enumerate(crops):
            dt, mode = screentime.find_screen_header(lines.get(i, []))
            if dt is not None and (out.get(p) or (None,))[0] is None:
                out[p] = (dt, mode)
            else:
                out.setdefault(p, (None, None))
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
