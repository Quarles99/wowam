# CLAUDE.md — wowam (WoW addon manager)

## What this is
wowam is a World of Warcraft addon manager that installs and auto-updates addons from the **official CurseForge Core API** (`https://api.curseforge.com`, `x-api-key` header).
It's built mainly for **WoW: Forever**, which launched 2026-11-04 and installs to `World of Warcraft/_forever_/Interface/AddOns`. Retail and Classic work too.
The owner is Will. He runs it on Windows and sometimes on Bazzite Linux (he dual-boots).

## Files
- `wowam.py` holds the core library and CLI. It uses only the Python **stdlib**, so add no third-party dependencies.
  - `CurseForge` is the API client: retries, backoff, and clear errors on 403 and 404.
  - `Manager` handles state, install, update, remove, scan, pin and flavor resolution.
  - `schedule()` / `unschedule()` set up the OS-level auto-update job: Task Scheduler `wowam-update` on Windows, a systemd user timer on Linux.
- `wowam_gui.py` is the Tkinter front-end. Network calls run on worker threads, and UI updates go through a queue drained by `root.after`. Never touch Tk widgets from a worker thread.
- `tests/mock_cf.py` is a fake CurseForge API. `tests/run_tests.sh` has 19 core checks and `tests/forever_tests.sh` has 6 Forever checks.
  Run both after any change: `bash tests/run_tests.sh && bash tests/forever_tests.sh`. They need bash, python3 and curl (use Git Bash or WSL on Windows).

## Key design decisions (keep these)
- **State is stored per install** in `Interface/wowam-state.json`, next to `AddOns`.
- **Config** is in `%APPDATA%\wowam\config.json` on Windows and `~/.config/wowam/config.json` on Linux. It holds the API key, which can also come from `WOWAM_API_KEY`.
  **Never commit an API key** or write one into this repo.
- **Flavor**: `flavor: "auto"` infers the flavor from the AddOns path (`_forever_` → "forever"; see `FOLDER_FLAVORS`).
  Names other than retail and classic are resolved live through `GET /v1/games/1/version-types`, matched by name and slug words. Beta and PTR types are skipped unless asked for.
  The resolved ID is cached in `config.flavor_cache`.
- **Picking a file**: use `mod.latestFilesIndexes` filtered by `gameVersionTypeId == flavor` and `releaseType <= channel`, and take the highest `fileId`.
  The fallback is `/v1/mods/{id}/files`, re-filtered client-side on `sortableGameVersions`. Don't trust the server-side filter alone; a test covers this.
- **Safe install**: extract to a staging dir inside `Interface/`, move old folders to `Interface/wowam-backup/<modId>/`, move new folders in, and roll back if anything fails. Zip entries with `..`, absolute paths or drive letters are refused.
- **Distribution opt-out**: when `downloadUrl` is null and `/download-url` returns null, report it to the user.
  **Do not** build `edge.forgecdn.net` URLs to get around it. That is the author's choice.
- **`scan`** adopts existing addons through the `## X-Curse-Project-ID` line in their `.toc` files. Adopted entries get `file_id: None`, so the next `update` reinstalls them once to sync.

## Status (handoff, 2026-11-05)
Done and passing against the mock: install by slug, ID or URL; required dependencies; update; per-addon channels; pin; remove; scan; zip-slip protection; backup and rollback; Forever flavor detection and resolution; GUI check and update flow, smoke-tested under Xvfb.

**Not yet verified against the real API.** The cloud sandbox it was built in couldn't reach `api.curseforge.com`. Will has a key; ask him for it, or he can run `python wowam.py config --api-key ...` himself.

### Next steps, in order
1. Check that Python 3.8+ is installed on Windows (`py --version`). If not, install it from python.org; it includes Tkinter.
2. Run `python wowam.py config --api-key <key>`, then `python wowam.py flavors`.
   - **Confirm the real name and ID of the WoW Forever version type.** `resolve_flavor("forever")` assumes it contains "forever". If CurseForge names it differently, add an alias in `FLAVOR_ALIASES` with the real ID.
3. Run `python wowam.py detect`. Find where Will's WoW is installed (Battle.net default: `C:\Program Files (x86)\World of Warcraft\_forever_\Interface\AddOns`). If it's on another drive or path, make sure `detect_addons_dirs()` covers it.
4. Run `scan`, `check` and `update` on his real Forever AddOns folder. Look for:
   - response fields that differ from the mock (`latestFilesIndexes`, `sortableGameVersions`, `modules`)
   - addons whose Forever build lives in a separate CurseForge project, and whether `scan` handles them
   - Windows file-lock errors when moving folders while WoW is running
5. Run `python wowam.py schedule --every 60`, and check that the Task Scheduler entry runs with `pythonw.exe` and no console window.
6. Optional: `python wowam.py gui`. Then think about a `wowam.bat` or a desktop shortcut for Will.

## Conventions
- Keep it stdlib-only and Python 3.8-compatible. `from __future__ import annotations` is already used for the type hints.
- User-facing errors raise `WowamError`; the CLI catches them and exits with code 2.
- Add a mock-server test for every behavior change. Extend `tests/mock_cf.py` rather than mocking inside Python.
