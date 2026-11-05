# CLAUDE.md — wowam (WoW addon manager)

## What this is
wowam is a World of Warcraft addon manager that installs and auto-updates addons from the **official CurseForge Core API** (`https://api.curseforge.com`, `x-api-key` header), and from **GitHub release zips** for addons whose authors block third-party downloads on CurseForge.
It's built mainly for **WoW: Forever**, which launched 2026-11-04 and installs to `World of Warcraft/_forever_/Interface/AddOns`. Retail and Classic work too.
The owner is Will. He runs it on Windows and sometimes on Bazzite Linux (he dual-boots).

## Files
- `wowam.py` holds the core library and CLI. It uses only the Python **stdlib**, so add no third-party dependencies.
  - `CurseForge` is the API client: retries, backoff, and clear errors on 403 and 404.
  - `Manager` handles state, install, update, remove, scan, pin and flavor resolution, plus `install_local` (hand-downloaded zips) and `install_github`.
  - `schedule()` / `unschedule()` set up the OS-level auto-update job: Task Scheduler `wowam-update` on Windows, a systemd user timer on Linux.
- `wowam_gui.py` is the Tkinter front-end. Network calls run on worker threads, and UI updates go through a queue drained by `root.after`. Never touch Tk widgets from a worker thread.
- `wowam.bat` is the Windows launcher: it verifies Python really runs (the Microsoft Store `python` stub doesn't count), offers a winget install if Python is missing, opens the GUI with no arguments, and otherwise forwards arguments. Keep it CRLF (`.gitattributes` enforces it).
- `tests/mock_cf.py` is a fake CurseForge + GitHub API. `tests/run_tests.sh` has the core checks, `tests/forever_tests.sh` the Forever checks, and `tests/test_rollback.py` (run from `run_tests.sh`) checks that a failed folder swap restores the old version.
  Run both scripts after any change: `bash tests/run_tests.sh && bash tests/forever_tests.sh`. They need bash, python3 and curl (use Git Bash or WSL on Windows).
  On Windows, `python3` is usually the Microsoft Store stub; put a shim for the real `python.exe` first on `PATH`.
  The "detects Lutris install" check looks at Linux paths and is expected to fail on Windows.

## Key design decisions (keep these)
- **State is stored per install** in `Interface/wowam-state.json`, next to `AddOns`. Entries are keyed by CurseForge mod ID, or `gh:owner/repo` for GitHub installs.
  `source: "github"` marks GitHub entries (tracked by release `tag`); `manual: true` marks zips installed by hand (with `toc_version`).
  When all of an entry's folders move to another entry, the empty entry is dropped.
- **Config** is in `%APPDATA%\wowam\config.json` on Windows and `~/.config/wowam/config.json` on Linux. It holds the API key, which can also come from `WOWAM_API_KEY`.
  **Never commit an API key** or write one into this repo.
- **Tests must never touch the real config or AddOns.** `WOWAM_CONFIG_DIR` overrides the config directory; both test scripts set it and pin `--addons-dir` explicitly.
  (`XDG_CONFIG_HOME` alone only isolates Linux. Early test runs overwrote Will's real Windows config and installed mock addons into his real Forever folder.)
  Never run an ad-hoc script against the real config either; use `WOWAM_CONFIG_DIR`, or override `addons_dir` in memory only.
- **Flavor**: `flavor: "auto"` infers the flavor from the AddOns path (`_forever_` → "forever"; see `FOLDER_FLAVORS`).
  Names other than retail and classic are resolved live through `GET /v1/games/1/version-types`, matched by name and slug words. Beta and PTR types are skipped unless asked for.
  The resolved ID is cached in `config.flavor_cache`. Verified against the real API: **WoW Forever = 88568** (slug `wow-forever`); no alias was needed.
- **Picking a file**: use `mod.latestFilesIndexes` filtered by `gameVersionTypeId == flavor` and `releaseType <= channel`, and take the highest `fileId`.
  The fallback is `/v1/mods/{id}/files`, re-filtered client-side on `sortableGameVersions`. Don't trust the server-side filter alone; a test covers this.
- **Safe install**: extract to a staging dir inside `Interface/`, move old folders to `Interface/wowam-backup/<id>/` (`gh:` ids are sanitized), move new folders in, and roll back if anything fails. Zip entries with `..`, absolute paths or drive letters are refused.
  **Folder moves must be atomic renames** (`_move_dir`). `shutil.move` falls back to copy-then-delete when a rename is refused (WoW holding a file open on Windows), which once left a live addon half-deleted while claiming it was restored.
- **Distribution opt-out**: when `downloadUrl` is null and `/download-url` returns null, raise `ManualDownload` with the addon's CurseForge page and the `install-file` hint.
  **Do not** build `edge.forgecdn.net` URLs to get around it. That is the author's choice. Details!, DBM, Auctionator, Leatrix Plus and others are opted out.
  The supported routes are: the author's GitHub release zips (`install --github`, fully automatic), or a zip the user downloads from CurseForge themselves (`install-file`).
- **Manual installs** (`install_local`): the project comes from `X-Curse-Project-ID` in the zip's `.toc`, else the `X-Website` CurseForge slug, else `--mod`. The zip is matched to a CurseForge file by file name; failing that, `toc_version` is compared with the latest file's name.
  `check`/`update` report manual addons' newer versions with a link instead of failing (`blocked` in `check_updates`), and never count them as failures.
- **`scan`** adopts existing addons through the `## X-Curse-Project-ID` line in their `.toc` files. Adopted entries get `file_id: None`, so the next `update` reinstalls them once to sync (blocked addons are compared by `.toc` version instead).

## Real-API findings
- Will's key can read games, mods, files, categories and `mods/featured`, but **`/v1/mods/search` returns 403**. Slug and name lookups therefore fail for him; numeric IDs work. Errors say so. Another key may behave differently.
- A `cfc_pat_…` personal access token is not a Core API key (403 everywhere).
- `mods/featured` with `gameVersionTypeId` 88568 is a handy way to list Forever addons and their `allowModDistribution` flag.
- DBM's GitHub release zips declare a `forever` flavor (interface 16001); `release.json` lists the flavors.

## Status (2026-11-05)
Verified against the real API on Will's Forever install: flavor resolution, `detect`, install by ID, check/update, GitHub install (DBM), manual install (Auctionator), real update with backup, and rollback with a locked file. The GUI was smoke-tested on Windows against the mock.

Still open:
- `wowam schedule --every 60` has never been created; check that the Task Scheduler entry runs with `pythonw.exe` and no console window.
- The new features have only been run on Windows, not on Bazzite.
- Details!, Auctionator and Leatrix Plus publish no GitHub release zips, so they stay on the manual `install-file` route.
- Possible next features: Wago/WoWInterface sources (check their terms first), a desktop shortcut for the GUI.

## Conventions
- Keep it stdlib-only and Python 3.8-compatible (`from __future__ import annotations` is used for type hints; no backslashes inside f-string expressions).
- User-facing errors raise `WowamError`; the CLI catches them and exits with code 2.
- Add a mock-server test for every behavior change. Extend `tests/mock_cf.py` rather than mocking inside Python.
- Don't hold secrets in the repo or in logs; Will's API key lives only in his config file.
