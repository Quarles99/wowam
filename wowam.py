#!/usr/bin/env python3
"""
wowam — World of Warcraft addon manager backed by the official CurseForge API.

Pure Python standard library (3.8+). No pip installs required.

Quick start:
    python wowam.py config --api-key YOUR_KEY          # from console.curseforge.com
    python wowam.py detect                             # find your AddOns folder(s)
    python wowam.py config --addons-dir "/path/to/_retail_/Interface/AddOns"
    python wowam.py scan                               # adopt addons you already have
    python wowam.py install details                    # by slug, id, or CurseForge URL
    python wowam.py update                             # update everything
    python wowam.py schedule --every 60                # auto-update every 60 min
"""
from __future__ import annotations

import argparse
import errno
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

__version__ = "1.0.0"

API_BASE = os.environ.get("WOWAM_API_BASE", "https://api.curseforge.com").rstrip("/")
WOW_GAME_ID = 1
USER_AGENT = f"wowam/{__version__}"

# CurseForge gameVersionTypeIds. "retail" (517) is stable; for anything else run
# `wowam flavors` to list the live IDs from the API, and pass the number.
FLAVOR_ALIASES = {"retail": 517, "mainline": 517, "classic": 67408, "classic_era": 67408, "era": 67408}
# Install-folder name -> flavor name, used to infer the flavor from the AddOns path.
# Flavors not in FLAVOR_ALIASES (e.g. "forever") are looked up live via the API.
FOLDER_FLAVORS = {"_retail_": "retail", "_forever_": "forever", "_forever_beta_": "forever beta",
                  "_classic_era_": "classic", "_ptr_": "retail", "_beta_": "retail"}


def flavor_from_path(addons_dir) -> str | None:
    for part in Path(addons_dir).parts:
        if part.lower() in FOLDER_FLAVORS:
            return FOLDER_FLAVORS[part.lower()]
    return None
CHANNELS = {"release": 1, "beta": 2, "alpha": 3}
RELEASE_NAMES = {1: "release", 2: "beta", 3: "alpha"}
REQUIRED_DEPENDENCY = 3


class WowamError(Exception):
    pass


class ManualDownload(WowamError):
    """The author disabled third-party downloads; the user has to fetch the zip themselves."""


GITHUB_API = os.environ.get("WOWAM_GITHUB_API", "https://api.github.com").rstrip("/")


def _github_latest(repo: str) -> dict:
    """Latest non-prerelease GitHub release for 'owner/repo'. GITHUB_TOKEN (optional) raises the rate limit."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    req = urllib.request.Request(f"{GITHUB_API}/repos/{repo}/releases/latest", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise WowamError(f"{repo}: no GitHub releases found (the author may distribute elsewhere).")
        if e.code in (403, 429):
            raise WowamError("GitHub rate limit hit; try again later or set GITHUB_TOKEN.")
        raise WowamError(f"GitHub API error {e.code} for {repo}")
    except urllib.error.URLError as e:
        raise WowamError(f"Network error talking to GitHub: {e.reason}")


def _move_dir(src: Path, dst: Path) -> None:
    """Move a folder with an atomic rename, so it either fully moves or doesn't move at all.
    (shutil.move falls back to copy-then-delete when a rename is refused, e.g. because WoW has a file
    open on Windows, and that can leave the source half-deleted.) Only a cross-device move may copy."""
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.move(str(src), str(dst))


def _version_in(version: str, text: str) -> bool:
    """True if `version` appears in `text` as a whole token (so '2.1' doesn't match '2.10')."""
    version = (version or "").strip().lstrip("vV")
    return bool(version) and bool(re.search(rf"(?<![\w.]){re.escape(version)}(?![\w.])", text or "", re.I))


# --------------------------------------------------------------------------- config

def config_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = Path(os.environ["WOWAM_CONFIG_DIR"]) if os.environ.get("WOWAM_CONFIG_DIR") else base / "wowam"
    d.mkdir(parents=True, exist_ok=True)
    return d


CONFIG_PATH = config_dir() / "config.json"
DEFAULT_CONFIG = {
    "api_key": "",
    "addons_dir": "",
    "flavor": "auto",  # auto = infer from the AddOns path (_retail_, _forever_, ...)
    "channel": "release",
    "install_dependencies": True,
    "auto_check_minutes": 60,
    "auto_update": False,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))  # -sig: tolerate editors' BOM
        except json.JSONDecodeError as e:
            raise WowamError(f"Config file is corrupt ({CONFIG_PATH}): {e}")
    if os.environ.get("WOWAM_API_KEY"):
        cfg["api_key"] = os.environ["WOWAM_API_KEY"]
    return cfg


def save_config(cfg: dict) -> None:
    _atomic_write_json(CONFIG_PATH, cfg)
    try:
        os.chmod(CONFIG_PATH, 0o600)  # holds the API key
    except OSError:
        pass


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- AddOns detection

def detect_addons_dirs() -> list[Path]:
    """Look in the usual places for WoW AddOns folders (Windows, Wine/Proton prefixes, macOS)."""
    home = Path.home()
    roots = []
    if os.name == "nt":
        for drive in "CDEFG":
            roots += [f"{drive}:/Program Files (x86)/World of Warcraft",
                      f"{drive}:/Program Files/World of Warcraft",
                      f"{drive}:/World of Warcraft",
                      f"{drive}:/Games/World of Warcraft"]
    elif sys.platform == "darwin":
        roots += ["/Applications/World of Warcraft"]
    else:
        prefixes = [
            home / "Games" / "*",                                   # Lutris default
            home / ".wine",
            home / ".local/share/bottles/bottles/*",                # Bottles
            home / ".var/app/com.usebottles.bottles/data/bottles/bottles/*",
            home / "Faugus" / "*",                                  # Faugus Launcher
            home / ".steam/steam/steamapps/compatdata/*/pfx",       # Steam/Proton
            home / ".local/share/Steam/steamapps/compatdata/*/pfx",
            home / ".var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/compatdata/*/pfx",
            home / ".var/app/net.lutris.Lutris/data/lutris/prefixes/*",
        ]
        for p in prefixes:
            for sub in ("drive_c/Program Files (x86)/World of Warcraft",
                        "drive_c/Program Files/World of Warcraft"):
                roots.append(str(p / sub))
    found = []
    for r in roots:
        for root in glob.glob(r):
            for d in glob.glob(os.path.join(root, "_*_", "Interface", "AddOns")):
                p = Path(d)
                if p.is_dir() and p not in found:
                    found.append(p)
    return found


# --------------------------------------------------------------------------- TOC parsing

TOC_FIELD = re.compile(r"^##\s*([^:]+):\s*(.*)$")


def read_toc(folder: Path) -> dict:
    """Return the ## fields of an addon folder's .toc (first one found)."""
    tocs = sorted(folder.glob("*.toc"), key=lambda p: (p.stem != folder.name, len(p.name)))
    for toc in tocs:
        fields = {}
        try:
            text = toc.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = TOC_FIELD.match(line.strip())
            if m:
                fields.setdefault(m.group(1).strip(), m.group(2).strip())
        return fields
    return {}


# --------------------------------------------------------------------------- API client

class CurseForge:
    def __init__(self, api_key: str, timeout: int = 30):
        if not api_key:
            raise WowamError(
                "No CurseForge API key set. Get one at https://console.curseforge.com "
                "then run: wowam config --api-key YOUR_KEY  (or set WOWAM_API_KEY)")
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, path: str, params: dict | None = None, body=None, retries: int = 3):
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        headers = {"x-api-key": self.api_key, "Accept": "application/json", "User-Agent": USER_AGENT}
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    raise WowamError("CurseForge rejected the API key (403). Check it with `wowam config --api-key`.")
                if e.code == 404:
                    raise WowamError(f"Not found: {path}")
                if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise WowamError(f"CurseForge API error {e.code} on {path}")
            except urllib.error.URLError as e:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise WowamError(f"Network error talking to CurseForge: {e.reason}")
        raise WowamError("Unreachable")

    # -- endpoints
    def version_types(self):
        return self._request(f"/v1/games/{WOW_GAME_ID}/version-types")["data"]

    def search(self, term: str = "", flavor: int | None = None, slug: str | None = None, page_size: int = 20):
        return self._request("/v1/mods/search", {
            "gameId": WOW_GAME_ID, "searchFilter": term or None, "slug": slug,
            "gameVersionTypeId": flavor, "sortField": 2, "sortOrder": "desc", "pageSize": page_size,
        })["data"]

    def mod(self, mod_id: int):
        return self._request(f"/v1/mods/{mod_id}")["data"]

    def mods(self, mod_ids: list[int]):
        out = []
        for i in range(0, len(mod_ids), 100):
            out += self._request("/v1/mods", body={"modIds": mod_ids[i:i + 100], "filterPcOnly": True})["data"]
        return out

    def mod_files(self, mod_id: int, flavor: int | None, page_size: int = 50):
        return self._request(f"/v1/mods/{mod_id}/files",
                             {"gameVersionTypeId": flavor, "pageSize": page_size})["data"]

    def files(self, file_ids: list[int]):
        return self._request("/v1/mods/files", body={"fileIds": file_ids})["data"]

    def download_url(self, mod_id: int, file_id: int):
        try:
            return self._request(f"/v1/mods/{mod_id}/files/{file_id}/download-url").get("data")
        except WowamError:
            return None

    def download(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return r.read()
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 2:
                    raise WowamError(f"Download failed: {e}")
                time.sleep(2 ** attempt)
        raise WowamError("Unreachable")


# --------------------------------------------------------------------------- manager

class Manager:
    def __init__(self, cfg: dict, log=print):
        self.cfg = cfg
        self.log = log
        if not cfg.get("addons_dir"):
            raise WowamError("AddOns folder not set. Run `wowam detect`, then `wowam config --addons-dir PATH`.")
        self.addons = Path(cfg["addons_dir"]).expanduser()
        if not self.addons.is_dir():
            raise WowamError(f"AddOns folder does not exist: {self.addons}")
        self.interface = self.addons.parent
        self.state_path = self.interface / "wowam-state.json"
        self.backup_root = self.interface / "wowam-backup"
        self.cf = CurseForge(cfg.get("api_key", ""))
        self.flavor = self.resolve_flavor(cfg.get("flavor", "auto"))
        self.channel = CHANNELS.get(str(cfg.get("channel", "release")).lower(), 1)
        self.state = self._load_state()

    # -- state
    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.state_path.rename(self.state_path.with_suffix(".corrupt.json"))
                self.log("! State file was corrupt; moved aside and starting fresh (run `wowam scan`).")
        return {"version": 1, "addons": {}}

    def save_state(self):
        _atomic_write_json(self.state_path, self.state)

    @property
    def installed(self) -> dict:
        return self.state["addons"]

    # -- helpers
    def resolve_flavor(self, value) -> int:
        s = str(value).strip().lower()
        if s in ("", "auto"):
            s = flavor_from_path(self.addons) or "retail"
        if s.isdigit():
            return int(s)
        if s in FLAVOR_ALIASES:
            return FLAVOR_ALIASES[s]
        cache = self.cfg.setdefault("flavor_cache", {})
        if s in cache:
            return cache[s]
        words = s.replace("_", " ").split()
        hits = [vt for vt in self.cf.version_types()
                if all(w in (vt.get("name", "") + " " + vt.get("slug", "")).lower().replace("_", " ") for w in words)]
        if not hits:
            raise WowamError(f"Unknown flavor '{value}'. Run `wowam flavors` to see valid IDs.")
        # "forever" should pick "WoW Forever", not "WoW Forever Beta"/PTR, unless asked for.
        noisy = lambda vt: any(t in vt.get("name", "").lower() and t not in s for t in ("beta", "ptr", "test"))
        best = sorted(hits, key=lambda vt: (noisy(vt), len(vt.get("name", ""))))[0]
        cache[s] = best["id"]
        try:  # remember it so later runs skip the lookup
            if CONFIG_PATH.exists():
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                raw.setdefault("flavor_cache", {})[s] = best["id"]
                save_config(raw)
        except (OSError, json.JSONDecodeError):
            pass
        self.log(f"Using CurseForge flavor '{best.get('name')}' (id {best['id']}).")
        return best["id"]

    def find_entry(self, ref: str):
        """Locate an installed addon by mod id, slug, name or folder name."""
        ref_l = ref.lower()
        for mid, e in self.installed.items():
            if ref_l in (mid, e.get("slug", "").lower(), e.get("name", "").lower()) or \
               ref_l in (f.lower() for f in e.get("folders", [])):
                return mid, e
        matches = [(m, e) for m, e in self.installed.items() if ref_l in e.get("name", "").lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise WowamError(f"'{ref}' is ambiguous: " + ", ".join(e["name"] for _, e in matches))
        raise WowamError(f"No installed addon matches '{ref}'.")

    def resolve_mod(self, ref: str) -> dict:
        """Turn an id, slug or CurseForge URL into a mod object."""
        ref = ref.strip()
        m = re.search(r"curseforge\.com/wow/addons/([^/?#]+)", ref)
        if m:
            ref = m.group(1)
        if ref.isdigit():
            return self.cf.mod(int(ref))
        try:
            hits = self.cf.search(slug=ref, page_size=5)
        except WowamError as e:
            if "403" in str(e):  # some API keys can read mods but not search
                raise WowamError(f"Can't look up '{ref}' by name: this API key isn't allowed to search CurseForge. "
                                 f"Use the numeric project ID instead (shown as 'Project ID' on the addon's page).")
            raise
        hits = [h for h in hits if h.get("slug") == ref]
        if hits:
            return hits[0]
        hits = self.cf.search(term=ref, flavor=self.flavor, page_size=5)
        if not hits:
            raise WowamError(f"No CurseForge addon found for '{ref}'.")
        exact = [h for h in hits if h["name"].lower() == ref.lower()]
        if exact:
            return exact[0]
        raise WowamError(f"'{ref}' is not an exact slug. Did you mean: "
                         + ", ".join(f"{h['slug']} ({h['name']})" for h in hits))

    def latest_file_id(self, mod: dict, channel: int | None = None) -> int | None:
        channel = channel or self.channel
        cands = [i for i in mod.get("latestFilesIndexes", [])
                 if i.get("gameVersionTypeId") == self.flavor and i.get("releaseType", 1) <= channel]
        if cands:
            return max(i["fileId"] for i in cands)
        # Fallback: query the file list directly
        def for_flavor(f):  # don't trust the server-side filter alone
            sgv = f.get("sortableGameVersions")
            return not sgv or any(v.get("gameVersionTypeId") == self.flavor for v in sgv)
        files = [f for f in self.cf.mod_files(mod["id"], self.flavor)
                 if f.get("releaseType", 1) <= channel and f.get("isAvailable", True) and for_flavor(f)]
        return max((f["id"] for f in files), default=None)

    # -- install / update core
    def _extract(self, blob: bytes, mod_name: str) -> tuple[Path, list[str]]:
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile:
            raise WowamError(f"{mod_name}: downloaded file is not a valid zip")
        staging = Path(tempfile.mkdtemp(prefix=".wowam-", dir=self.interface))
        folders = set()
        try:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                parts = [p for p in name.split("/") if p not in ("", ".")]
                if not parts or parts[0] == "__MACOSX":
                    continue
                if name.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", name):
                    raise WowamError(f"{mod_name}: zip contains an unsafe path ({info.filename}); refusing")
                if len(parts) == 1 and not info.is_dir():
                    continue  # stray file at zip root (README etc.)
                folders.add(parts[0])
                target = staging.joinpath(*parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if not folders:
            shutil.rmtree(staging, ignore_errors=True)
            raise WowamError(f"{mod_name}: zip contains no addon folders")
        return staging, sorted(folders)

    def _swap_in(self, mod_id: str, staging: Path, new_folders: list[str], old_folders: list[str]):
        backup = self.backup_root / re.sub(r"[^\w.-]", "_", mod_id)  # 'gh:owner/repo' isn't a legal dir name
        shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir(parents=True, exist_ok=True)
        moved, placed = [], []
        try:
            for f in sorted(set(old_folders) | set(new_folders)):
                live = self.addons / f
                if live.exists():
                    _move_dir(live, backup / f)
                    moved.append(f)
            for f in new_folders:
                _move_dir(staging / f, self.addons / f)
                placed.append(f)
        except Exception as e:
            for f in placed:  # our fresh copies
                shutil.rmtree(self.addons / f, ignore_errors=True)
            for f in moved:
                if (backup / f).exists() and not (self.addons / f).exists():
                    _move_dir(backup / f, self.addons / f)
            hint = " Close WoW (or whatever has the addon's files open) and try again." \
                if isinstance(e, PermissionError) else ""
            raise WowamError(f"Failed to install files ({e}); previous version restored.{hint}")
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def install_file(self, mod: dict, file: dict, pinned: bool = False, channel: str | None = None):
        url = file.get("downloadUrl") or self.cf.download_url(mod["id"], file["id"])
        if not url:
            raise ManualDownload(
                f"{mod['name']}: the author has disabled third-party downloads for this addon. "
                f"Download it yourself from {mod.get('links', {}).get('websiteUrl') or 'its CurseForge page'}, "
                f"then run: wowam install-file <downloaded.zip>  (wowam will keep checking it for updates).")
        self.log(f"  ↓ {mod['name']}  {file.get('displayName') or file.get('fileName')}")
        staging, new_folders = self._extract(self.cf.download(url), mod["name"])
        self._commit(mod, staging, new_folders, file=file, pinned=pinned, channel=channel)

    def _commit(self, mod: dict, staging: Path, new_folders: list[str], file: dict | None = None,
                pinned: bool = False, channel: str | None = None, version: str | None = None,
                extra: dict | None = None):
        """Swap extracted folders in (with backup/rollback) and record the addon in the state file."""
        mod_id = str(mod["id"])
        old = self.installed.get(mod_id, {}).get("folders", [])

        # Folders another tracked addon owned now belong to this one.
        for other_id, other in list(self.installed.items()):
            if other_id != mod_id:
                overlap = set(other.get("folders", [])) & set(new_folders)
                if overlap:
                    other["folders"] = [f for f in other["folders"] if f not in overlap]
                    self.log(f"  ! {', '.join(sorted(overlap))} moved from {other['name']} to {mod['name']}")
                    if not other["folders"]:  # nothing left: it's been replaced (e.g. GitHub -> hand-installed)
                        del self.installed[other_id]
                        self.log(f"  ! {other['name']} no longer owns any folders; stopped tracking it.")
        self._swap_in(mod_id, staging, new_folders, old)

        prev = self.installed.get(mod_id, {})
        file = file or {}
        self.installed[mod_id] = {
            "name": mod["name"], "slug": mod.get("slug", ""),
            "file_id": file.get("id"), "version": version or file.get("displayName") or file.get("fileName"),
            "file_date": file.get("fileDate", ""),
            "release_type": RELEASE_NAMES.get(file["releaseType"]) if file.get("releaseType") else None,
            "folders": new_folders, "pinned": pinned or prev.get("pinned", False),
            "channel": channel or prev.get("channel"), "installed_at": int(time.time()),
            "url": mod.get("links", {}).get("websiteUrl", ""),
        }
        self.installed[mod_id].update(extra or {})
        self.save_state()

    def install_local(self, path: str, mod_ref: str | None = None) -> dict:
        """Install a zip the user downloaded by hand (for addons whose author blocks third-party downloads).
        The CurseForge project is read from X-Curse-Project-ID in the .toc unless mod_ref is given."""
        p = Path(path).expanduser()
        if not p.is_file():
            raise WowamError(f"No such file: {p}")
        staging, folders = self._extract(p.read_bytes(), p.name)
        try:
            tagged: dict[str, list[str]] = {}  # project id -> folders carrying it
            for f in folders:
                pid = read_toc(staging / f).get("X-Curse-Project-ID", "").strip()
                if pid.isdigit():
                    tagged.setdefault(pid, []).append(f)
            if not mod_ref and not tagged:  # some zips only carry the CurseForge page URL
                for f in folders:
                    site = read_toc(staging / f).get("X-Website", "")
                    if re.search(r"curseforge\.com/wow/addons/[^/?#\s]+", site):
                        mod_ref = site
                        break
            if mod_ref:
                mod = self.resolve_mod(mod_ref)
            elif tagged:
                mod = self.cf.mod(int(max(tagged, key=lambda k: len(tagged[k]))))
            else:
                raise WowamError(f"{p.name}: no X-Curse-Project-ID in its .toc files; "
                                 f"say which addon it is with --mod <slug|id|url>.")
            try:  # identify the exact CurseForge file by its file name, so updates can be detected
                files = self.cf.mod_files(mod["id"], self.flavor)
            except WowamError:
                files = []
            match = next((f for f in files if f.get("fileName", "").lower() == p.name.lower()), None)
            toc_ver = read_toc(staging / tagged.get(str(mod["id"]), folders)[0]).get("Version", "")
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self.log(f"  ↓ {mod['name']}  {(match or {}).get('displayName') or p.name}  (from {p})")
        self._commit(mod, staging, folders, file=match,
                     version=None if match else f"{toc_ver or p.stem} (manual)",
                     extra={"manual": True, "toc_version": toc_ver})
        return self.installed[str(mod["id"])]

    def install_github(self, spec: str, asset_pattern: str | None = None, pinned: bool = False) -> None:
        """Install/update from the latest GitHub release zip of 'owner/repo' (or a github.com URL)."""
        m = re.search(r"github\.com/([^/\s]+)/([^/\s?#]+)", spec) or re.fullmatch(r"([\w.-]+)/([\w.-]+)", spec.strip())
        if not m:
            raise WowamError(f"'{spec}' is not a GitHub repo (use owner/repo or a github.com URL).")
        name = re.sub(r"\.git$", "", m.group(2))
        repo = f"{m.group(1)}/{name}"
        rel = _github_latest(repo)
        zips = [a for a in rel.get("assets", []) if a.get("name", "").lower().endswith(".zip")]
        if asset_pattern:
            zips = [a for a in zips if re.search(asset_pattern, a["name"], re.I)]
        if not zips:
            raise WowamError(f"{repo} {rel.get('tag_name')}: the release has no matching .zip asset "
                             f"(the author may not publish addon zips on GitHub).")
        if len(zips) > 1:
            raise WowamError(f"{repo} {rel.get('tag_name')} has several zips ({', '.join(a['name'] for a in zips)}); "
                             f"pick one with --asset <regex>.")
        asset, tag = zips[0], rel.get("tag_name") or rel.get("name") or "?"
        key = f"gh:{repo}"
        mod = {"id": key, "name": repo.split("/")[1], "slug": repo.split("/")[1].lower(),
               "links": {"websiteUrl": f"https://github.com/{repo}"}}
        self.log(f"  ↓ {mod['name']}  {tag}  (GitHub {repo})")
        staging, folders = self._extract(self.cf.download(asset["browser_download_url"]), mod["name"])
        self._commit(mod, staging, folders, pinned=pinned, version=tag,
                     extra={"source": "github", "repo": repo, "tag": tag, "asset_pattern": asset_pattern,
                            "file_date": rel.get("published_at", "")})

    def install(self, ref: str, channel: str | None = None, _seen=None) -> None:
        _seen = _seen if _seen is not None else set()
        mod = self.resolve_mod(ref)
        if mod["id"] in _seen:
            return
        _seen.add(mod["id"])
        ch = CHANNELS[channel] if channel else self.channel
        fid = self.latest_file_id(mod, ch)
        if not fid:
            raise WowamError(f"{mod['name']} has no {RELEASE_NAMES[ch]}-or-better file for this game flavor yet "
                             f"(try --channel beta, or check its CurseForge page for a separate Forever project).")
        file = self.cf.files([fid])[0]
        self.install_file(mod, file, channel=channel)
        if self.cfg.get("install_dependencies", True):
            for dep in file.get("dependencies", []):
                if dep.get("relationType") == REQUIRED_DEPENDENCY and str(dep["modId"]) not in self.installed:
                    self.log(f"  + dependency of {mod['name']}:")
                    try:
                        self.install(str(dep["modId"]), _seen=_seen)
                    except WowamError as e:
                        self.log(f"  ! could not install dependency {dep['modId']}: {e}")

    def check_updates(self) -> list[dict]:
        """Return [{id, entry, mod, latest_file_id, latest_version, blocked}] for addons with a newer file.

        `blocked` means the author disallows third-party downloads (or it was installed by hand), so
        wowam can report the update but the user has to download the zip and run `install-file`."""
        out, synced = [], False
        ids = [int(i) for i in self.installed if i.isdigit()]
        for mod in (self.cf.mods(ids) if ids else []):
            mid = str(mod["id"])
            entry = self.installed.get(mid)
            if not entry or entry.get("pinned"):
                continue
            ch = CHANNELS.get(entry.get("channel") or "", self.channel)
            fid = self.latest_file_id(mod, ch)
            if not fid:
                continue
            blocked = bool(entry.get("manual") or mod.get("allowModDistribution") is False)
            latest = self.cf.files([fid])[0] if (blocked or fid != entry.get("file_id")) else {}
            if blocked and entry.get("file_id") is None:
                # Installed by hand and not matched to a CurseForge file: compare the .toc version instead.
                if _version_in(entry.get("toc_version", ""), f"{latest.get('displayName')} {latest.get('fileName')}"):
                    entry["file_id"], synced = fid, True
                    continue
            if fid != entry.get("file_id"):
                out.append({"id": mid, "entry": entry, "mod": mod, "latest_file_id": fid, "blocked": blocked,
                            "latest_version": latest.get("displayName") or latest.get("fileName") or str(fid)})
        for key, entry in list(self.installed.items()):
            if entry.get("source") != "github" or entry.get("pinned"):
                continue
            tag = _github_latest(entry["repo"]).get("tag_name")
            if tag and tag != entry.get("tag"):
                out.append({"id": key, "entry": entry, "mod": None, "latest_file_id": None, "blocked": False,
                            "latest_version": tag})
        if synced:
            self.save_state()
        return out

    def manual_notice(self, p: dict) -> str:
        url = p["entry"].get("url") or (p["mod"] or {}).get("links", {}).get("websiteUrl", "")
        return (f"{p['entry']['name']}: {p['entry'].get('version')}  →  {p['latest_version']} "
                f"(manual download: {url}, then `wowam install-file <zip>`)")

    def update(self, refs: list[str] | None = None) -> tuple[int, list[str]]:
        pending = self.check_updates()
        if refs:
            wanted = {self.find_entry(r)[0] for r in refs}
            pending = [p for p in pending if p["id"] in wanted]
        if not pending:
            self.log("Everything is up to date.")
            return 0, []
        manual = [p for p in pending if p["blocked"]]
        auto = [p for p in pending if not p["blocked"]]
        for p in manual:
            self.log(f"  ! needs manual download — {self.manual_notice(p)}")
        files = {}
        cf_ids = [p["latest_file_id"] for p in auto if p["latest_file_id"]]
        if cf_ids:
            files = {f["id"]: f for f in self.cf.files(cf_ids)}
        ok, failed = 0, []
        for p in auto:
            try:
                if p["entry"].get("source") == "github":
                    self.install_github(p["entry"]["repo"], p["entry"].get("asset_pattern"))
                else:
                    self.install_file(p["mod"], files[p["latest_file_id"]])
                ok += 1
            except ManualDownload as e:  # author blocked it after all: same as a manual addon
                self.log(f"  ! {e}")
            except (WowamError, KeyError) as e:
                failed.append(f"{p['entry']['name']}: {e}")
                self.log(f"  ! {p['entry']['name']}: {e}")
        self.log(f"Updated {ok} addon(s)" + (f", {len(failed)} failed." if failed else ".")
                 + (f" {len(manual)} need a manual download." if manual else ""))
        return ok, failed

    def remove(self, ref: str):
        mid, entry = self.find_entry(ref)
        for f in entry.get("folders", []):
            shutil.rmtree(self.addons / f, ignore_errors=True)
        del self.installed[mid]
        self.save_state()
        self.log(f"Removed {entry['name']} ({', '.join(entry.get('folders', []))})")

    def set_pinned(self, ref: str, pinned: bool):
        mid, entry = self.find_entry(ref)
        entry["pinned"] = pinned
        self.save_state()
        self.log(f"{'Pinned' if pinned else 'Unpinned'} {entry['name']}")

    def scan(self) -> tuple[int, list[str]]:
        """Adopt addons already in the AddOns folder using the X-Curse-Project-ID in their .toc."""
        tracked = {f for e in self.installed.values() for f in e.get("folders", [])}
        by_project: dict[str, list[str]] = {}
        unknown = []
        for d in sorted(p for p in self.addons.iterdir() if p.is_dir()):
            if d.name in tracked or d.name.startswith("Blizzard_"):
                continue
            pid = read_toc(d).get("X-Curse-Project-ID", "").strip()
            if pid.isdigit():
                by_project.setdefault(pid, []).append(d.name)
            else:
                unknown.append(d.name)
        if not by_project:
            self.log("No new CurseForge-tagged addons found.")
            return 0, unknown
        mods = {str(m["id"]): m for m in self.cf.mods([int(p) for p in by_project])}
        added = 0
        for pid, folders in by_project.items():
            mod = mods.get(pid)
            if not mod:
                unknown += folders
                continue
            toc_ver = read_toc(self.addons / folders[0]).get("Version", "?")
            entry = self.installed.setdefault(pid, {
                "name": mod["name"], "slug": mod.get("slug", ""), "file_id": None,
                "version": f"{toc_ver} (unverified)", "file_date": "", "release_type": None,
                "folders": [], "pinned": False, "channel": None, "installed_at": 0,
                "url": mod.get("links", {}).get("websiteUrl", ""), "toc_version": toc_ver,
            })
            entry["folders"] = sorted(set(entry["folders"]) | set(folders))
            added += 1
            self.log(f"  + {mod['name']}  [{', '.join(folders)}]")
        self.save_state()
        self.log(f"Adopted {added} addon(s). Their exact version is unknown, so the next "
                 f"update will reinstall the latest file once to sync them.")
        if unknown:
            self.log(f"Not on CurseForge / not tagged (left alone): {', '.join(unknown)}")
        return added, unknown


# --------------------------------------------------------------------------- scheduling

def _self_cmd() -> list[str]:
    return [sys.executable, str(Path(__file__).resolve())]


def schedule(every_minutes: int) -> str:
    cmd = _self_cmd() + ["update", "--quiet"]
    if os.name == "nt":
        exe = cmd[0].replace("python.exe", "pythonw.exe")  # no console window popping up
        tr = f'"{exe}" "{cmd[1]}" update --quiet'
        subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", str(every_minutes),
                        "/TN", "wowam-update", "/TR", tr], check=True)
        return f"Windows Task Scheduler task 'wowam-update' runs every {every_minutes} min."
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        exec_line = " ".join(f'"{c}"' for c in cmd)
        (unit_dir / "wowam-update.service").write_text(
            "[Unit]\nDescription=Update WoW addons from CurseForge\n"
            "Wants=network-online.target\nAfter=network-online.target\n\n"
            f"[Service]\nType=oneshot\nExecStart={exec_line}\n")
        (unit_dir / "wowam-update.timer").write_text(
            "[Unit]\nDescription=Periodic WoW addon updates\n\n"
            f"[Timer]\nOnBootSec=3min\nOnUnitActiveSec={every_minutes}min\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "wowam-update.timer"], check=True)
        return (f"systemd user timer 'wowam-update.timer' runs every {every_minutes} min. "
                f"Logs: journalctl --user -u wowam-update")
    return ("Automatic scheduling isn't supported on this OS. Add this to cron/launchd instead:\n  "
            + " ".join(cmd))


def unschedule() -> str:
    if os.name == "nt":
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", "wowam-update"], check=False)
        return "Removed scheduled task."
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", "wowam-update.timer"], check=False)
        unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
        for n in ("wowam-update.service", "wowam-update.timer"):
            (unit_dir / n).unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        return "Removed systemd timer."
    return "Nothing to remove."


# --------------------------------------------------------------------------- CLI

def _fmt_table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    line = lambda r: "  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip()
    return "\n".join([line(headers), line(["-" * w for w in widths])] + [line(r) for r in rows])


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # piped Windows output is cp1252; don't die on arrows/accents
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(prog="wowam", description="WoW addon manager (CurseForge)")
    ap.add_argument("--addons-dir", help="override the AddOns folder for this run")
    ap.add_argument("--flavor", help="override game flavor for this run (retail, classic, or numeric ID)")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("config", help="show or change settings")
    c.add_argument("--api-key"); c.add_argument("--addons-dir", dest="set_dir")
    c.add_argument("--flavor", dest="set_flavor"); c.add_argument("--channel", choices=CHANNELS)
    c.add_argument("--deps", choices=["on", "off"], help="auto-install required dependencies")

    sub.add_parser("detect", help="find WoW AddOns folders on this machine")
    sub.add_parser("flavors", help="list CurseForge WoW game-flavor IDs")
    s = sub.add_parser("search", help="search CurseForge"); s.add_argument("term", nargs="+")
    i = sub.add_parser("install", help="install by slug, id or URL (or --github owner/repo)")
    i.add_argument("refs", nargs="+")
    i.add_argument("--channel", choices=CHANNELS, help="per-addon channel (e.g. beta)")
    i.add_argument("--github", action="store_true", help="refs are GitHub 'owner/repo' (latest release zip)")
    i.add_argument("--asset", help="regex to pick the release zip when a GitHub release has several")
    f = sub.add_parser("install-file", help="install a zip you downloaded by hand (author blocks third-party downloads)")
    f.add_argument("files", nargs="+"); f.add_argument("--mod", help="slug/id/URL if the zip has no X-Curse-Project-ID")
    sub.add_parser("list", help="list installed addons")
    sub.add_parser("check", help="check for updates without installing")
    u = sub.add_parser("update", help="update all (or named) addons"); u.add_argument("refs", nargs="*")
    u.add_argument("--quiet", action="store_true")
    r = sub.add_parser("remove", help="uninstall an addon"); r.add_argument("refs", nargs="+")
    p = sub.add_parser("pin", help="freeze an addon at its current version"); p.add_argument("refs", nargs="+")
    p = sub.add_parser("unpin", help="allow updates again"); p.add_argument("refs", nargs="+")
    sub.add_parser("scan", help="adopt addons already in your AddOns folder")
    w = sub.add_parser("watch", help="stay running and update on an interval")
    w.add_argument("--every", type=int, default=60, help="minutes (default 60)")
    sc = sub.add_parser("schedule", help="install an OS-level auto-update job")
    sc.add_argument("--every", type=int, default=60, help="minutes (default 60)")
    sub.add_parser("unschedule", help="remove the auto-update job")
    sub.add_parser("gui", help="open the graphical manager")

    a = ap.parse_args(argv)
    cfg = load_config()
    if a.addons_dir:
        cfg["addons_dir"] = a.addons_dir
    if a.flavor:
        cfg["flavor"] = a.flavor

    try:
        if a.cmd == "config":
            changed = False
            for key, val in (("api_key", a.api_key), ("addons_dir", a.set_dir),
                             ("flavor", a.set_flavor), ("channel", a.channel)):
                if val is not None:
                    cfg[key] = val; changed = True
            if a.deps:
                cfg["install_dependencies"] = a.deps == "on"; changed = True
            if changed:
                save_config(cfg)
            shown = dict(cfg)
            if shown.get("api_key"):
                shown["api_key"] = shown["api_key"][:6] + "…" + f" ({len(shown['api_key'])} chars)"
            print(f"Config: {CONFIG_PATH}")
            print(json.dumps(shown, indent=2))
            return 0
        if a.cmd == "detect":
            dirs = detect_addons_dirs()
            if not dirs:
                print("No WoW install found in the usual places. Point wowam at it manually:\n"
                      "  wowam config --addons-dir \".../World of Warcraft/_retail_/Interface/AddOns\"")
                return 1
            for d in dirs:
                print(f"{d}    [{flavor_from_path(d) or 'unknown flavor'}]")
            if not cfg.get("addons_dir"):
                for pref in ("_forever_", "_retail_"):
                    pick = [d for d in dirs if pref in d.parts]
                    if len(pick) == 1:
                        cfg["addons_dir"] = str(pick[0]); save_config(cfg)
                        print(f"\nSaved as your AddOns folder: {pick[0]}")
                        break
                else:
                    print("\nPick one with: wowam config --addons-dir PATH")
            return 0
        if a.cmd == "unschedule":
            print(unschedule()); return 0
        if a.cmd == "gui":
            from wowam_gui import run_gui
            return run_gui()
        if a.cmd == "flavors":
            for vt in CurseForge(cfg.get("api_key", "")).version_types():
                print(f"{vt['id']:>7}  {vt.get('name', '')}  ({vt.get('slug', '')})")
            return 0
        if a.cmd == "search":
            cf = CurseForge(cfg.get("api_key", ""))
            m = Manager(cfg, log=lambda *_: None) if cfg.get("addons_dir") else None
            flavor = m.flavor if m else FLAVOR_ALIASES.get(str(cfg.get("flavor")), None)
            hits = cf.search(" ".join(a.term), flavor=flavor)
            rows = [[h["id"], h["slug"], h["name"][:40], f"{h.get('downloadCount', 0):,.0f}",
                     "manual" if h.get("allowModDistribution") is False else "auto",
                     "yes" if m and str(h["id"]) in m.installed else ""] for h in hits]
            print(_fmt_table(rows, ["ID", "SLUG", "NAME", "DOWNLOADS", "DOWNLOAD", "INSTALLED"]) if rows else "No results.")
            manual = [h for h in hits if h.get("allowModDistribution") is False]
            if manual:
                print("\nThe author blocks third-party downloads for these ('manual'). Download the zip from the "
                      "page, then run `wowam install-file <zip>`:")
                for h in manual:
                    print(f"  {h['name']}: {h.get('links', {}).get('websiteUrl', '')}")
            return 0

        log = (lambda *_: None) if getattr(a, "quiet", False) else print
        m = Manager(cfg, log=log)

        if a.cmd == "install":
            for ref in a.refs:
                try:
                    if a.github or "github.com/" in ref:
                        m.install_github(ref, asset_pattern=a.asset)
                    else:
                        m.install(ref, channel=a.channel)
                except WowamError as e:
                    print(f"! {e}", file=sys.stderr)
        elif a.cmd == "install-file":
            for path in a.files:
                m.install_local(path, mod_ref=a.mod)
        elif a.cmd == "list":
            src = lambda e: e.get("source") or ("manual" if e.get("manual") else "curseforge")
            rows = [[e["name"][:36], (e.get("version") or "")[:40], e.get("release_type") or "?", src(e),
                     "pinned" if e.get("pinned") else "", mid]
                    for mid, e in sorted(m.installed.items(), key=lambda kv: kv[1]["name"].lower())]
            print(_fmt_table(rows, ["NAME", "VERSION", "TYPE", "SOURCE", "", "ID"]) if rows else
                  "Nothing installed yet. Try `wowam scan` or `wowam install <slug>`.")
        elif a.cmd == "check":
            pending = m.check_updates()
            if not pending:
                print("Everything is up to date.")
            for p in pending:
                print(m.manual_notice(p) if p["blocked"] else
                      f"{p['entry']['name']}: {p['entry'].get('version')}  →  {p['latest_version']}")
        elif a.cmd == "update":
            _, failed = m.update(a.refs or None)
            return 1 if failed else 0
        elif a.cmd == "remove":
            for ref in a.refs:
                m.remove(ref)
        elif a.cmd in ("pin", "unpin"):
            for ref in a.refs:
                m.set_pinned(ref, a.cmd == "pin")
        elif a.cmd == "scan":
            m.scan()
        elif a.cmd == "watch":
            print(f"Watching for updates every {a.every} min (Ctrl+C to stop).")
            while True:
                try:
                    print(time.strftime("[%H:%M] "), end="")
                    Manager(cfg, log=print).update()
                except WowamError as e:
                    print(f"! {e}")
                time.sleep(a.every * 60)
        elif a.cmd == "schedule":
            print(schedule(a.every))
        return 0
    except WowamError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
