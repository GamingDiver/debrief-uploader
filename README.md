# GamingDiver Debrief Uploader (Windows)

Watches the game's `replays` folder and your screenshots folder, works out
which post-battle scorecards belong to which battle, and uploads the matched
set to [Debrief](https://gamingdiver.com/wowslegends/replays/). You never drag
a file.

It also **rescues replays the game is about to delete.** World of Warships
Legends keeps only your ~10 most recent battles and silently deletes the rest;
this copies every new one somewhere safe the moment it appears, before it does
anything else.

## Download

Get `DebriefUploader.zip` from the
[latest release](https://github.com/GamingDiver/debrief-uploader/releases/latest)
and unzip it anywhere.

## Install

Needs Python 3.9+ ([python.org](https://www.python.org/downloads/) — during
setup, **tick "Add python.exe to PATH"**).

**Double-click `Start-DebriefUploader.cmd`.** The first run builds its own
Python environment and finds your folders; then it puts an icon in your system
tray and **the window closes**. Everything after that lives in the tray icon.

Click the tray icon and choose **Sign in...** to finish setup.

### The tray icon

| | |
|---|---|
| **Sign in...** | shown until you are signed in; nothing uploads before then |
| **Review (n)** | resolve anything it would not guess at |
| **Status...** | what it has seen and uploaded, with links |
| **Settings...** | account, folders, visibility, review mode, scan limits, start-at-logon |
| **Why isn't it uploading?** | the full diagnosis, same as `Debrief.cmd doctor` |
| **Check now** | don't wait out the poll interval |
| **Pause / Resume**, **Quit** | |

**Start automatically at logon** is a checkbox in Settings. (The older
`Install-Uploader.ps1` still works and does the same thing from PowerShell.)

**To build a standalone `.exe`** that needs no Python at all — run this *on the
Windows PC*, since PyInstaller bundles the interpreter of the machine it runs
on and cannot cross-compile:

```powershell
powershell -ExecutionPolicy Bypass -File .\Build-Exe.ps1
```

Output is `dist\DebriefUploader.exe`.

## Using it

Use `Debrief.cmd <command>`, or `python -m debrief_uploader <command>`:

```
python -m debrief_uploader status     what it has seen and uploaded
python -m debrief_uploader review     resolve anything it wasn't sure about
python -m debrief_uploader run        watch and upload (add --tray for the tray icon)
python -m debrief_uploader setup      show or set the folders it watches
python -m debrief_uploader login      sign in (--email for email + password)
python -m debrief_uploader logout     sign out and forget the tokens
```

Play as normal. At the results screen, capture the **Personal** and **Team
Result** tabs (Steam `F12`, or `Win`+`PrtScn`). That's it — the pairing and the
upload happen on their own.

**Capture the whole screen, not a dragged box.** The site reads the scoreboard
off your screenshot, and a cropped region loses the rows at the bottom.

## What it does, in order

1. **Quarantines** every new replay to
   `%LOCALAPPDATA%\GamingDiver\DebriefUploader\staging` — before anything else,
   so the game's rotation can never destroy one it has seen.
2. Asks the site what the battle was (the replay is encrypted; the app never
   holds the key).
3. Groups your screenshots into per-battle bursts and matches each burst to the
   battle it followed.
4. Uploads the replay and its scorecards, then shows you the link.

Training-room battles upload straight away — they have no scorecard screens.

A battle you took no scorecards for uploads anyway once the replay is at least
15 minutes old. You still get the full debrief; only the scorecard-checked
figures are missing. Screenshots taken more than 15 minutes after a battle
closes are no longer matched to it. To keep scorecard-less replays on this PC
instead, set `"upload_without_scorecards": false` in `settings.json`.

## When it asks you something

It will not guess. If you skip a battle's screenshots, the next battle's
scorecards become genuinely ambiguous — the results screen for a battle you
left early can appear *after* the next battle has already finished. When that
happens it holds the upload and asks, rather than attaching your scorecards to
the wrong battle.

`python -m debrief_uploader review` clears the queue, or use **Review** in the
tray menu.

## Notes

- **Nothing is deleted.** Your screenshots are only ever read. Staged replays
  are kept forever by default.
- **It paces itself:** one upload at a time, 20 per hour by default. A backfill
  of a few hundred replays will not overwhelm the site.
- **Tokens** are stored encrypted with Windows DPAPI, scoped to your user
  account. Sign out removes them.
- **Logs** are in `%LOCALAPPDATA%\GamingDiver\DebriefUploader\logs`, kept 7
  days. File names and battle info only — no tokens, no images.
- **Settings**: `settings.json` next to the logs. Timings, folders, visibility
  for new uploads, and `review_mode` (hold everything for confirmation — worth
  turning on for your first session).

### Training rooms

Training-room battles upload as **private** by default. They are practice, and
unlike a real battle they carry no scorecard, so there is nothing for them to
add to the Base XP research and a fair chance you would rather they were not
listed publicly.

Change it under **Settings... → Uploads → Training-room battles**, or set
`training_visibility` in `settings.json` to `private`, `fleet`, `public`, or
`""` to treat them the same as everything else. Real battles are unaffected
either way.

### Screenshot scanning

Your screenshots folder is a lifetime archive, and none of last month's files
can belong to a battle that finished minutes ago. Three settings keep the scan
cheap and current:

| Setting | Default | What it does |
|---|---|---|
| `shot_max_age_hours` | `4` | Ignore screenshots older than this entirely |
| `shot_min_kb` | `1000` | Skip anything smaller, without opening it |
| `shot_scan_limit` | `25` | Most new screenshots taken in one pass |

Files are examined **newest first**, so the battle that just finished is never
queued behind an archive.

> **If you capture with Steam's `F12`**, it writes JPEG, which can land under
> 1 MB at 1080p — lower `shot_min_kb` to about `200`. Windows captures
> (`Win`+`PrtScn`, Snipping Tool) are PNG and comfortably over it.

### How quickly it decides

After your last screenshot it waits a moment in case you take another, then
matches and uploads:

| Setting | Default | When it applies |
|---|---|---|
| `pair_settle_full` | `30` s | Two or more screenshots — looks like a finished capture |
| `pair_settle` | `90` s | A single screenshot — still waiting for the other tab |

The log tells you the exact time it will decide, so a wait never looks like a
stall.

Raising `shot_max_age_hours` is safe; it only widens what gets looked at, and
matching still refuses anything more than 25 minutes from its battle.

## Requirements

`Pillow` (image transform) and `pystray` (tray icon only). Everything else is
the standard library.

## Development

```
python -m unittest discover -s tests
```

The matching engine is pure logic with no I/O, so the whole of it is testable
without a game, a PC, or a network. `tests/test_corpus_timing.py` additionally
replays the real archive's battle cadence through it when the archive is
present, and `tests/test_images.py` checks the scorecard transform against the
arithmetic extracted from the site's own `js/replay-upload.js` — so if the site
changes its crop, the test fails instead of the OCR quietly drifting.

## Development

```
pip install -r requirements.txt
python -m unittest discover -s tests
bash package.sh          # tests, window tests, then builds DebriefUploader.zip
```

Releases are built by GitHub Actions: bump `__version__` in
`debrief_uploader/__init__.py`, then push a matching tag (`v1.3.2`).

## License

MIT, see [LICENSE](LICENSE). GamingDiver and Debrief are not affiliated with
Wargaming; World of Warships: Legends is a trademark of Wargaming.
