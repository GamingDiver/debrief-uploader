# GamingDiver Debrief Uploader (Windows)

Watches the game's `replays` folder and your screenshots folder, works out
which post-battle scorecards belong to which battle, and uploads the matched
set to [Debrief](https://gamingdiver.com/wowslegends/replays/). You never drag
a file.

It also **rescues replays the game is about to delete.** World of Warships
Legends keeps only your ~10 most recent battles and silently deletes the rest;
this copies every new one somewhere safe the moment it appears, before it does
anything else.

## How to use it

Once it is installed and signed in, there is nothing else to do between
battles:

1. **Play the game as normal.**
2. **At the post-game screens, take a screenshot of each of the first two
   tabs**, *Personal* and *Team Result* (Steam `F12`, or `Win`+`PrtScn`).
   Capture the whole screen, not a dragged box.
3. **The Debrief Uploader automatically detects your replay and screenshots
   and uploads them** for review on
   [GamingDiver Debrief](https://gamingdiver.com/wowslegends/replays/). The
   link to each battle appears under **Ready for review** in the tray icon's
   **Status...** window.

## Install

You need Windows 10 or 11, the game, and a free
[GamingDiver](https://gamingdiver.com) account to upload to.

### 1. Install Python (one time)

1. Download Python 3.9 or newer from
   [python.org/downloads](https://www.python.org/downloads/).
2. Run the installer and, on its first screen, **tick "Add python.exe to
   PATH"** before clicking *Install Now*. Without it the uploader cannot find
   Python.

### 2. Download the uploader

1. Download `DebriefUploader.zip` from the
   [latest release](https://github.com/GamingDiver/debrief-uploader/releases/latest).
2. **Unblock the zip before you unzip it.** Windows marks every file
   downloaded from the internet, and on PCs with **Smart App Control** turned
   on it then refuses to run the launcher outright ("Smart App Control
   blocked a file that may be unsafe", with no *Run anyway* button).
   Right-click `DebriefUploader.zip` -> **Properties** -> at the bottom of the
   *General* tab, tick **Unblock** -> **OK**. Unblocking the zip first clears
   every file inside it in one go.
3. Unzip it somewhere **permanent**, for example
   `C:\Users\<you>\Documents\DebriefUploader`. Not your Downloads folder and
   not inside the zip preview: Windows will start it from this folder at every
   logon, so it must not move or be cleaned up later.

### 3. Start it

1. Open the unzipped folder and **double-click `Start-DebriefUploader.cmd`**.
2. If Windows still stops it:
   - *"Smart App Control blocked a file that may be unsafe"*: the zip was
     not unblocked. Click **OK**, then right-click
     `Start-DebriefUploader.cmd` -> **Properties** -> tick **Unblock** ->
     **OK**, and double-click it again. (Or unblock the whole folder at once:
     open PowerShell in the folder and run
     `Get-ChildItem -Recurse | Unblock-File`.)
   - *"Windows protected your PC"* (SmartScreen): click **More info**, then
     **Run anyway**.
3. The first run takes about a minute: it builds its own private Python
   environment and finds your replay and screenshot folders. Then the black
   window closes and a **diver icon appears in the system tray** (bottom
   right; click the `^` arrow if it is hidden).
4. Click the tray icon and choose **Sign in...**, then sign in the same way
   you do on gamingdiver.com:
   - **Sign in with Google** or **Sign in with Discord** if that is how you
     made your account (most people). Your browser opens; sign in there, and
     when it says *Signed in* you can close the tab. The uploader never sees
     your Google or Discord password.
   - **Email and password** only if your account has a password.

   Nothing uploads until you have signed in.

Double-clicking the launcher again is safe: if it is already running, the
second copy just exits.

### 4. Choose who can see your battles

Before your first battle, decide who can watch what you upload: tray icon ->
**Settings...** -> **Uploads** -> **Visibility for new uploads**.

| Setting | Who can watch the battle |
|---|---|
| **Public** (the default) | Anyone with the link, and it is listed in the Debrief community list. Your gamertag and your fleet's name show on it. |
| **Fleet** | Only signed-in members of your fleet. |
| **Private** | Only you. |
| **Use my site default** | Whatever you chose on the [Debrief page](https://gamingdiver.com/wowslegends/replays/) under *Visibility for new uploads*. That is **Public** unless you changed it. |

Good to know:

- **You can change it per battle later.** Open the battle's page on
  GamingDiver and pick a different visibility; making one private takes it
  out of the community list straight away.
- **Training rooms are separate** and upload as **Private** by default
  (*Training-room battles*, just below). See [Training rooms](#training-rooms).
- **Both sides of a battle can be merged.** When a teammate or an opponent
  uploads the same battle, Debrief can combine both views into one fuller
  replay, but never in a way that shows your upload to more people than you
  chose: a Public upload can be merged into any other; a Fleet upload only
  into uploads from the same fleet that are Fleet or Private; a Private
  upload never.
- Site admins can open any upload, including private ones, but only to fix
  problems with the site.

### 5. Start it in the background with Windows

So you never have to remember to start it before playing.

**Recommended: one click in Settings**

Tray icon -> **Settings...** -> under **Startup**, tick **Start
automatically when I log in**. It takes effect the moment you tick it (the
line underneath confirms "will start when you log in").

That's it. From then on it starts silently when you log in to Windows: no
window, not even a flash, just the diver icon in the tray. It registers a
per-user scheduled task named *GamingDiver Debrief Uploader* (no admin rights
needed), and the checkbox always shows whether that task really exists. Untick
it to stop. (`Install-Uploader.ps1` does the same from PowerShell.)

**Alternative: the Startup folder**

If you would rather manage startup apps yourself, a shortcut in your Startup
folder works too:

1. Press `Win`+`R`, type `shell:startup`, press Enter. Your personal Startup
   folder opens.
2. In a second Explorer window, open the uploader folder, **right-click
   `Start-DebriefUploader.cmd`** and choose **Show more options -> Create
   shortcut** (Windows 11) or **Create shortcut** (Windows 10).
3. Drag the new shortcut into the Startup folder.
4. Right-click the shortcut in the Startup folder -> **Properties** -> set
   **Run** to **Minimized** -> OK, so logon shows no black window.

Or create the same shortcut by running this in PowerShell **from inside the
uploader folder**:

```powershell
$s = (New-Object -ComObject WScript.Shell).CreateShortcut("$([Environment]::GetFolderPath('Startup'))\Debrief Uploader.lnk")
$s.TargetPath = "$PWD\Start-DebriefUploader.cmd"; $s.WorkingDirectory = "$PWD"; $s.WindowStyle = 7; $s.Save()
```

To undo it, delete the shortcut (or switch it off under *Settings -> Apps ->
Startup*).

Using both is harmless: the second copy sees the first and exits.

Either way, check it worked by signing out of Windows and back in: the diver
icon should appear in the tray on its own.

### Updating

Quit from the tray icon, unzip the new release **over the same folder**, and
start it again. Your sign-in, settings and upload history live in
`%LOCALAPPDATA%\GamingDiver\DebriefUploader`, not in the app folder, so they
carry over, and the Startup shortcut keeps working because the folder did not
move.

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

**Start automatically when I log in** is a checkbox in Settings; see
[Start it in the background with Windows](#5-start-it-in-the-background-with-windows).

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
python -m debrief_uploader login      sign in in the browser (--provider google|discord, or --email)
python -m debrief_uploader logout     sign out and forget the tokens
```

Day to day, follow [How to use it](#how-to-use-it): play, screenshot the
first two post-game tabs, and the pairing and the upload happen on their own.

**Capture the whole screen, not a dragged box.** The site reads the scoreboard
off your screenshot, and a cropped region loses the rows at the bottom. If you
forget the screenshots, the battle still uploads once the replay is 15 minutes
old; only the scorecard-checked figures are missing.

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
pip install -r requirements.txt
python -m unittest discover -s tests
bash package.sh          # tests, window tests, then builds DebriefUploader.zip
```

The matching engine is pure logic with no I/O, so the whole of it is testable
without a game, a PC, or a network. `tests/test_corpus_timing.py` additionally
replays a real archive's battle cadence through it when one is present
(`~/WowsLegendsReplayArchive`). `tests/test_images.py` checks the scorecard
transform against the arithmetic in the site's own upload code; those
browser-parity cases run when `GD_SITE_JS` points at the site's
`js/replay-upload.js` (GamingDiver's deploy runs them on every site change),
so if the site changes its crop, a test fails instead of the OCR quietly
drifting.

Releases are built by GitHub Actions: bump `__version__` in
`debrief_uploader/__init__.py`, then push a matching tag (`v1.3.2`).

## License

MIT, see [LICENSE](LICENSE). GamingDiver and Debrief are not affiliated with
Wargaming; World of Warships: Legends is a trademark of Wargaming.
