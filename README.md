# Nexus Mods Auto Downloader

Paste a Nexus Mods link, pick a folder, click **Download**. The tool downloads that mod **and every mod it
requires (recursively)** - or every mod of a **collection** - using your own logged-in Nexus account, one file at
a time, waiting out Nexus's free-user countdown.

> Unofficial tool. Not affiliated with or endorsed by Nexus Mods. Read "Please note" below before using it.

## Start it

Double-click **`run.bat`**. The first run creates a private Python environment and installs the two
dependencies (`playwright`, `requests`); after that it just opens the window. Needs Python 3.10+ and any
Chromium browser (Brave, Chrome or Edge - they are tried in that order).

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
| Skip files already downloaded | Uses `nexus_manifest.json` in the folder |
| Ask me before downloading | Shows the count, size and request cost first |
| Skip optional mods of collections | Leave out the entries a collection marks optional |
| Scan only | Lists everything needed and writes the report, downloads nothing |
| All main files | Also take every MAIN file of a mod, not only the author's primary file |
| Pause between downloads | Seconds to wait between two files (default 5) |
| Give up if a download does not start within | Covers the site's 5 s countdown (default 90 s) |

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

## Development

```
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

Layout: `urls.py` (link parsing) -> `nexus_api.py` (GraphQL: requirements, files, collections) -> `resolver.py`
(ordered plan) -> `job.py` (worker thread) which drives `browser.py` (attach to the browser), `site_flow.py` (the
Nexus download page) and `downloads.py` (captures the file through the browser's DevTools events); `budget.py`
enforces the request limits; `store.py` keeps settings, the manifest and the report; `app.py` is the Tkinter
window.
