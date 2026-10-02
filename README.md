# Nexus Mods Auto Downloader

Paste a Nexus Mods link, pick a folder, click **Download**. The tool downloads that mod **and every mod it
requires (recursively)** - or every mod of a **collection** - using your own logged-in Nexus account, one file at
a time, waiting out Nexus's free-user countdown.

> Unofficial tool. Not affiliated with or endorsed by Nexus Mods. Read "Please note" below before using it.

## Start it

Double-click **`run.bat`**. The first run creates a private Python environment and installs the two
dependencies (`playwright`, `requests`); after that it just opens the window. Needs Python 3.10+ and any
Chromium browser (Brave, Chrome or Edge - they are tried in that order).

No Python on the computer, or you want one file to hand around? [Build the .exe](#build-the-exe) - it has Python inside.

## Log in (once)

1. Press **Open browser / log in**. A separate browser window opens on the Nexus login page. It has its own
   profile, so your everyday browser is not touched.
2. Log in there the normal way (2FA, Google sign-in and captchas all work). The tool never sees your password.
3. Press **Check login**. The dot turns green. The login is kept in that profile, so you only do this once.

## Download

1. Paste one or more links, one per line:
   - a **mod page**: `https://www.nexusmods.com/<game>/mods/<id>`
   - a **collection**: `https://www.nexusmods.com/games/<game>/collections/<slug>`
     (add `/revisions/<number>` for a specific revision; otherwise the latest published one is used)
2. Choose the download folder.
3. Press **Download**. The tool first reads what is needed from Nexus's API and shows it (mods, sizes, off-site
   requirements, how many requests it will cost). Confirm, and it starts downloading.

Everything lands in one flat folder, so frameworks shared by many mods (Cyber Engine Tweaks, RED4ext,
ArchiveXL ...) are downloaded once. Running it again skips what is already there.

### Collections

A collection lists the exact file of every mod its author chose, so those exact files are downloaded (not each
mod's latest file) and, like Vortex, requirements are **not** followed on top: a collection already contains what
it needs. Optional mods are included unless you tick **Skip optional mods of collections**. Things a collection
needs from outside Nexus are listed in the table and the report for you to get yourself.

Nexus offers automatic collection downloads to **Premium** members. On a free account this tool has to fetch
every file one by one, so big collections (hundreds of files) are exactly the bulk automation Nexus's rules
warn about, and the confirmation says so. Prefer Premium with Vortex for those.

| Option | Meaning |
|---|---|
| Also download requirements | Follow each mod's requirements recursively (switch off to get only the mods you pasted) |
| Skip files already downloaded | Keeps intact files as they are (see "Duplicates and corrupted files"); untick it to download everything again |
| Ask me before downloading | Shows the count, size and request cost first |
| Skip optional mods of collections | Leave out the entries a collection marks optional |
| Scan only | Lists everything needed and writes the report, downloads nothing |
| All main files | Also take every MAIN file of a mod, not only the author's primary file |
| Pause between downloads | Seconds to wait between two files (default 5) |
| Give up if a download does not start within | Covers the site's 5 s countdown (default 90 s) |

## Delete files, download them again, clear the log

The file table has actions for each entry. **Right-click a row**, or select one or more rows and use the buttons
above the table (the **Delete** key works too):

- **Delete file** - quick delete: removes the downloaded file of the selected row(s) and forgets it. One row is
  deleted at once without a question; several rows ask first. The row stays in the table (status "deleted"), so
  you can bring the file back with the next action.
- **Download again** - downloads the selected file(s) again without scanning Nexus again. A copy that is already
  there is only replaced once the new download is complete (a failed attempt keeps the old file), a corrupted copy
  is deleted first, and the request budget and countdown apply as always. One file starts at once; several ask
  first if "Ask me before downloading" is ticked. It also works on a row that failed or was deleted.

Two buttons work on the whole window:

- **Delete all downloaded files** - deletes every file this tool downloaded into the folder, i.e. the ones listed
  in the folder's `nexus_manifest.json`, after a confirmation that names the folder and the number and size of
  the files. Other files in the folder are never touched, and neither are the report and the manifest (which is
  emptied). Deleted files are gone for good: they do not go to the Recycle Bin.
- **Clear log** - empties the log window. (The log file of the last run, `last_run.log`, is kept until the next run.)

Off-site and unavailable rows have no file, so only their link can be opened (double-click or right-click).
These actions are disabled while a download, a deletion or a login check is in progress.

## Duplicates and corrupted files

Nothing is downloaded twice, and nothing corrupted is kept:

- **Already there:** before downloading, the tool looks at what is in the folder. A file it downloaded earlier
  (recorded in `nexus_manifest.json`) is skipped. A valid file it never recorded (manifest lost, or the file came
  from Vortex or a browser) is recognised by the name Nexus gives it and skipped too. Newer Nexus files do not
  expose their name, so those can only be recognised through the manifest.
- **Corrupted:** an existing file is checked every run: it must not be empty, must have the size it had when it
  was downloaded (or the size Nexus lists), and must be a readable archive (a zip's directory, the signature of
  a .7z/.rar). If it fails, the tool says why, **deletes it and downloads it again**. The file is only deleted
  right before its new download starts, so scanning, declining the confirmation or pressing Stop never deletes
  anything.
- **Fresh downloads** are tested before they are accepted: the size must match, and a zip is read completely and
  its checksums verified. A damaged download is deleted and retried (3 attempts); if it stays damaged, no file
  is left behind and it is reported as failed.
- **Download again on purpose** (untick "Skip files already downloaded"): the new file replaces the old one
  instead of being saved next to it as "name (1)".
- **Leftovers:** half-finished downloads from an earlier crash (browser-named temporary files) are removed when a
  run starts. Other files in your folder are never touched.

There is no content checksum from Nexus to compare against, so the check can tell a truncated or damaged archive
from a good one, but it cannot detect a file that was swapped for a different, valid archive of the same size.

## Request limits

Nexus limits requests to **20,000 per 24 hours** and **500 per hour**. The tool counts every request it makes
(its API calls and the page and API-style requests of the Nexus pages in its browser tab; a file costs about 9,
a whole collection scan costs 1), keeps the usage between runs, and **pauses by itself at 90 % of both limits**.
When the hourly limit is near it waits (you see a countdown in the table); if the daily limit is used up it stops
with a clear message, and the files already downloaded are remembered for the next run. The current usage is
shown in the window. In practice this caps a free account at roughly 45 files per hour.
The limits can be lowered, never raised, in `config.json` (`request_limit_hour`, `request_limit_day`).

## What it writes into your folder

- the downloaded archives, with their original Nexus file names
- `nexus_manifest.json` - what was downloaded (used to skip next time)
- `requirements_report.md` - collections, install order, dependency tree, **off-site requirements you must get
  yourself**, required game expansions, unavailable mods

Off-site requirements (hosted outside Nexus) are never downloaded; double-click their row in the table to open
the link. Hidden or removed mods are listed as unavailable.

## Your data

Nothing is sent anywhere except to Nexus Mods, and there is no telemetry. The tool stores:

| What | Where |
|---|---|
| Settings (folder, delays, options - no links, no keys) | `%LOCALAPPDATA%\NexusAutoDownloader\config.json` |
| Your Nexus login (the tool's own browser profile) | `%LOCALAPPDATA%\NexusAutoDownloader\browser-profile\` |
| How many requests were made recently | `%LOCALAPPDATA%\NexusAutoDownloader\request_usage.json` |
| Log of the last run (your home folder is shown as `~`) | `%LOCALAPPDATA%\NexusAutoDownloader\last_run.log` |
| Screenshots of pages where a download failed | `%LOCALAPPDATA%\NexusAutoDownloader\diagnostics\` |

All of it lives outside the program folder. The browser profile **is your Nexus login**: never share it, and
check screenshots before attaching them to a bug report (they can show your avatar and name).
While the tool's browser window is open, its debugging port (`127.0.0.1:9222`) can be used by programs running on
your computer, so close that window when you are not using the tool.
The tool does not use or ask for a Nexus API key: anonymous read access is enough.

## Troubleshooting

- **A verification check appears in the browser window**: complete it there; the tool waits and continues.
- **It says "not logged in" mid-run**: log in again in the browser window; it carries on.
- **"Could not find the download button"**: click it yourself in the browser window; the tool still captures the
  download. If this keeps happening Nexus changed its page; the markup the tool relies on is in one block at the
  top of `nexus_dl/site_flow.py`.
- **Adult mods**: Nexus only serves them to accounts that have "Show adult content" enabled in the site preferences.
- **An old collection revision fails for some files**: older revisions can pin files Nexus has since archived.

## Please note

Nexus Mods' terms prohibit downloading "in a fashion that drastically exceeds the expected average, through the
use of software automation or otherwise". This tool is built to stay well inside normal use: one file at a time,
the site's countdown respected, files never fetched twice, a request budget it will not exceed, and a confirmation
before it starts. Use it for your own downloads, at a sensible volume. Premium members get the site's real fast
downloads through the same flow.

Nexus's API policy asks every app to identify itself (this tool sends `Application-Name` and
`Application-Version`) and asks apps that reach a public audience to register with Nexus support. If you publish or
fork this tool, please do that.

## Build the .exe

Double-click **`build.bat`**. It makes `dist\NexusAutoDownloader.exe`: the program, Python itself and the libraries in
one file. Copy it anywhere; the computer that runs it needs no Python (only a browser, as always). Settings and login
stay in `%LOCALAPPDATA%\NexusAutoDownloader`, exactly as with `run.bat`.

- **Building needs Python 3.14.** If it is not installed, `build.bat` installs it from the `python-3.14.x-amd64.exe`
  that lies next to it, into a `.python` folder (this user only, no admin rights, PATH untouched; it is listed under
  Installed apps like any Python, where it can be uninstalled).
- **What it does:** makes a clean environment (`.build-venv`), installs the libraries and PyInstaller, packs
  everything, then **starts the new exe once** (`--self-test`) to prove that the window toolkit, the HTTPS certificates
  and Playwright's Node driver really are inside it. A build whose self-test fails is reported as failed. The first
  build takes a few minutes.
- `build.bat --onedir` makes a folder with the exe in it instead of one file. The single file unpacks about 135 MB
  into a temporary folder every time it starts (2 seconds on a fast disk, more on a slow one or while an antivirus
  scans it); the folder starts in about 1. `build.bat --console` adds a console window, to see why a build will not
  start.
- Windows SmartScreen and antivirus programs sometimes object to programs made with PyInstaller, because the exe is
  not code-signed. If yours removes it, allow the `dist` folder, or build with `--onedir`.

## Development

```
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

Layout: `urls.py` (link parsing) -> `nexus_api.py` (GraphQL: requirements, files, collections) -> `resolver.py`
(ordered plan) -> `job.py` (worker thread) which drives `browser.py` (attach to the browser), `site_flow.py` (the
Nexus download page) and `downloads.py` (captures the file through the browser's DevTools events); `verify.py`
checks archives for corruption; `cleanup.py` deletes downloaded files; `budget.py` enforces the request limits;
`store.py` keeps settings, the manifest and the report; `app.py` is the Tkinter window. `tools/build_exe.py` and
`tools/launcher.py` make the .exe (see above); `build.bat` finds or installs Python for them.
