# wowam — WoW Addon Manager (CurseForge)

A small, dependency-free WoW addon manager that uses the **official CurseForge API**.
It has a CLI and a GUI and works on Windows, Linux (including Wine/Proton/Lutris installs) and macOS.

## Easy install (no command line)

**Windows, easiest:** download `wowam.exe` from the [latest Release](../../releases/latest) and double-click it. No Python needed.
Windows may show a blue "Windows protected your PC" box because the file isn't code-signed.
Click **More info → Run anyway**. (If you'd like to check the download, `wowam.exe.sha256` on the Release page has its checksum.)

**Windows, from the source code:** click the green **Code** button → **Download ZIP**, unzip it, and double-click `wowam.bat`.
If you don't have Python, it offers to install it for you.

**Either way**, the first launch opens a short setup guide:
1. Get a free CurseForge key (it walks you through it and checks the key works).
2. Pick your World of Warcraft install (Retail, WoW Forever, Classic…). It finds them for you.
3. Choose whether to include the addons you already have, and whether to add a desktop shortcut.

**Updating wowam itself:** once a day wowam checks this repo's Releases for a newer version. With `wowam.exe` a banner
appears with **Update now**: it downloads the new `wowam.exe`, checks it against the Release's SHA-256 checksum, swaps it in
and restarts. Nothing changes if anything looks wrong. If you run from source, the banner just links to the download page.
Choose **Skip this version** to dismiss one release, or untick the option in Settings to stop checking.
(Self-update starts with the first release that includes it; older `wowam.exe` files need one manual download.)

After that, use **Browse / Install…** to see the most popular addons (or search),  **Check for updates** / **Update all**, and **Install from file…** for the few
addons whose authors don't allow outside downloads (see below).

## Setup from the command line (5 minutes)

1. **Get a CurseForge API key.** Sign in at <https://console.curseforge.com>, then go to *API keys*.
   New keys may need approval from CurseForge.
2. **Python 3.8+** is the only requirement. The CLI uses only the standard library.
   The GUI also needs Tkinter (`python3-tkinter` on Fedora/Bazzite; it's already included with Python on Windows).
3. Configure it:
   ```bash
   python wowam.py config --api-key YOUR_KEY
   python wowam.py detect          # finds _retail_/Interface/AddOns and saves it if there's one
   # or set it by hand:
   python wowam.py config --addons-dir "C:/Program Files (x86)/World of Warcraft/_retail_/Interface/AddOns"
   python wowam.py scan            # adopt the addons you already have
   ```
   `detect` checks the usual install locations (drives C–G on Windows, Wine/Proton/Lutris prefixes on Linux).
   If your game lives somewhere else, use the `--addons-dir` form above.

**Windows:** `wowam.bat` is a launcher that finds Python for you. Double-click it to open the GUI,
or run it from a terminal in place of `python wowam.py` (for example `wowam.bat check`).

## WoW Forever

Forever is fully supported. Point wowam at `World of Warcraft/_forever_/Interface/AddOns`; `detect` picks it automatically if it finds one.
wowam reads the game flavor from that folder name, so only addon files CurseForge has tagged for Forever get installed.
Retail and Classic files are skipped. If an addon has no Forever file yet, wowam tells you so instead of installing the wrong version.
To manage your retail install as well, add `--addons-dir ".../_retail_/Interface/AddOns"` to any command.

## Everyday use

| Command | What it does |
|---|---|
| `wowam browse` | Browse the most popular addons for your game version. `--sort updated` or `--sort downloads` change the order, `--page 2` goes further. The ID column is what you pass to `install` |
| `wowam search details` | Search CurseForge by name |
| `wowam install details bigwigs` | Install by slug, project ID or CurseForge URL. Required dependencies are installed too |
| `wowam install weakauras-2 --channel beta` | Follow beta (or alpha) builds for this addon only |
| `wowam check` | List the addons that have updates |
| `wowam update` | Update everything (or `wowam update details`) |
| `wowam install --github DeadlyBossMods/DeadlyBossMods` | Install from the latest GitHub release zip (also accepts a github.com URL; `--asset REGEX` picks one if a release has several zips). Updates are tracked by release tag |
| `wowam install-file Auctionator-341.zip` | Install a zip you downloaded yourself. See [Addons that block third-party downloads](#addons-that-block-third-party-downloads) |
| `wowam list` / `remove` / `pin` / `unpin` | Self-explanatory (`list` shows each addon's source: curseforge, github or manual) |
| `wowam gui` | Open the graphical manager |
| `wowam shortcut` | Put a desktop shortcut that opens the graphical manager |
| `wowam self-update` | Check for a newer wowam (`--check` only reports; with the packaged exe it installs it after asking, or with `--yes`) |
| `wowam config --theme dark` | GUI colors: `auto` (follows your system's dark/light setting, the default), `light` or `dark`. Also in the GUI's Settings |

Some API keys can't search by name, and CurseForge then answers 403. If that happens, use `wowam browse` (or the GUI's Browse window) to find the addon, and install by numeric project ID
(shown as "Project ID" on the addon's CurseForge page), for example `wowam install 18808`.

## Automatic updates

Choose one:

- **Background (recommended):** `wowam schedule --every 60` sets up a systemd user timer on Linux or a Task Scheduler job on Windows. It runs `wowam update` every hour, even when nothing else is open. Remove it with `wowam unschedule`.
  On Linux, logs go to `journalctl --user -u wowam-update`.
- **GUI:** in Settings, choose an auto-check interval and tick "Install updates automatically".
- **Foreground:** `wowam watch --every 30`

## How it works / safety

- State is stored next to your AddOns folder in `Interface/wowam-state.json`, so each WoW install (retail, classic) keeps its own list.
  To manage a second install, pass `--addons-dir ... --flavor classic`. Run `wowam flavors` to see the flavor IDs.
- Each update first downloads to a staging folder. The previous version's folders are then moved to `Interface/wowam-backup/<id>/`, and the new ones are moved in. If anything fails, the old version is restored.
- Folders that an old version had but the new one doesn't are cleaned up.
- Zips with path-traversal entries are refused.
- `scan` reads the `X-Curse-Project-ID` tag that CurseForge's packager writes into `.toc` files. Addons without that tag (WoWInterface/GitHub-only, or your own) are left alone. Because the exact version of a scanned addon isn't known, the first `update` after a scan reinstalls the latest file once.
## Addons that block third-party downloads

Some authors (Details!, Deadly Boss Mods, Auctionator, Leatrix Plus and others) turn off third-party distribution.
CurseForge then gives programs like wowam no download link, and wowam respects that rather than working around it.
You can still manage them:

1. `wowam install <addon>`, or "Install…" in the GUI, tells you the addon needs a manual download and gives its CurseForge page
   (the GUI opens it for you). `wowam search` marks such addons as `manual`.
2. Download the zip from that page yourself, then run `wowam install-file path/to/addon.zip`
   (GUI: "Install from file…"). wowam works out which addon it is from the zip's `.toc`.
   If the zip doesn't say, pass the project with `--mod <id|slug|url>`. The numeric ID is the one that always works.
3. After that wowam checks the addon for updates like any other. `wowam check` and `wowam update` show
   `Name: old → new (manual download: <page>)` when a newer file exists. Download it and run `install-file` again.
   The previous version is kept in `Interface/wowam-backup/`.

If the author also publishes release zips on GitHub (Deadly Boss Mods does), `wowam install --github owner/repo` gets
fully automatic updates instead. Set the `GITHUB_TOKEN` environment variable if you check many repos and hit GitHub's rate limit.

## Notes

The config file (containing your API key) is at `~/.config/wowam/config.json` on Linux and `%APPDATA%\wowam\config.json` on Windows.
You can also set the key with the `WOWAM_API_KEY` environment variable.

## Tests

`bash tests/run_tests.sh && bash tests/forever_tests.sh` runs everything against a local mock of the CurseForge API. No API key or network is needed.
