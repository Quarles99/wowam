# wowam — WoW Addon Manager (CurseForge)

A small, dependency-free WoW addon manager that uses the **official CurseForge API**.
It has a CLI and a GUI and works on Windows, Linux (including Wine/Proton/Lutris installs) and macOS.

## Setup (5 minutes)

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

## WoW Forever

Forever is fully supported. Point wowam at `World of Warcraft/_forever_/Interface/AddOns`; `detect` picks it automatically if it finds one.
wowam reads the game flavor from that folder name, so only addon files CurseForge has tagged for Forever get installed.
Retail and Classic files are skipped. If an addon has no Forever file yet, wowam tells you so instead of installing the wrong version.
To manage your retail install as well, add `--addons-dir ".../_retail_/Interface/AddOns"` to any command.

## Everyday use

| Command | What it does |
|---|---|
| `wowam search details` | Search CurseForge |
| `wowam install details bigwigs` | Install by slug, project ID or CurseForge URL. Required dependencies are installed too |
| `wowam install weakauras-2 --channel beta` | Follow beta (or alpha) builds for this addon only |
| `wowam check` | List the addons that have updates |
| `wowam update` | Update everything (or `wowam update details`) |
| `wowam list` / `remove` / `pin` / `unpin` | Self-explanatory |
| `wowam gui` | Open the graphical manager |

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
- Some authors turn off third-party distribution. CurseForge then gives no download link for that addon, and wowam tells you so instead of working around it. Use the CurseForge app for those addons.

The config file (containing your API key) is at `~/.config/wowam/config.json` on Linux and `%APPDATA%\wowam\config.json` on Windows.
You can also set the key with the `WOWAM_API_KEY` environment variable.

## Tests

`bash tests/run_tests.sh && bash tests/forever_tests.sh` runs everything against a local mock of the CurseForge API. No API key or network is needed.
