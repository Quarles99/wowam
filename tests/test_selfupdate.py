"""Self-update logic against the mock GitHub API (stdlib only). run_tests.sh starts the mock server and sets
WOWAM_GITHUB_API / WOWAM_CONFIG_DIR; this refuses to run without an isolated config."""
import os
import sys
import tempfile
from pathlib import Path

if not os.environ.get("WOWAM_CONFIG_DIR") or not os.environ.get("WOWAM_GITHUB_API"):
    sys.exit("refusing to run without WOWAM_CONFIG_DIR and WOWAM_GITHUB_API (would touch the real config)")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import wowam

problems = []


def check(name, cond):
    if not cond:
        problems.append(name)


def repo(r):
    wowam.UPDATE_REPO = r


# --- version comparison
t = wowam._version_tuple
check("v1.10.0 > 1.9.0", t("v1.10.0") > t("1.9.0"))
check("1.1.0 == v1.1.0", t("1.1.0") == t("v1.1.0"))
check("prerelease suffix ignored", t("1.2.0-beta") == (1, 2, 0))

# --- newer_release
repo("acme/selfupd")
info = wowam.newer_release()
check("newer release found", info and info["version"] == "9.9.9" and info["exe_url"] and info["sha_url"])
repo("acme/selfold")
check("older release ignored", wowam.newer_release() is None)

# --- daily check: throttle, opt-out, skip
repo("acme/selfupd")
cfg = {}
check("first check finds it", (wowam.check_self_update(cfg) or {}).get("version") == "9.9.9")
check("second check within a day is throttled", wowam.check_self_update(cfg) is None)
check("force bypasses the throttle", wowam.check_self_update(cfg, force=True) is not None)
check("check can be switched off", wowam.check_self_update({"check_for_updates": False}) is None)
check("skipped version is not offered", wowam.check_self_update({"skipped_version": "9.9.9"}) is None)
check("last check time persisted", '"last_update_check"' in wowam.CONFIG_PATH.read_text())

# --- applying: refused unless it's the packaged exe
repo("acme/selfupd")
info = wowam.newer_release()
wowam.FROZEN = False
try:
    wowam.apply_self_update(info, log=lambda *_: None)
    check("source install refuses to self-update", False)
except wowam.WowamError:
    pass

# --- applying: fake frozen exe in a temp dir
real_exe = sys.executable


def fresh_exe():
    d = Path(tempfile.mkdtemp())
    exe = d / "wowam.exe"
    exe.write_bytes(b"MZ-old-exe")
    wowam.FROZEN = True
    sys.executable = str(exe)
    return exe


try:
    exe = fresh_exe()
    wowam.apply_self_update(info, log=lambda *_: None)
    check("exe replaced with the release's", exe.read_bytes() == b"MZnew-wowam-exe-v9.9.9")
    check("previous exe kept as .old", exe.with_name("wowam.exe.old").read_bytes() == b"MZ-old-exe")
    check("no .new left behind", not exe.with_name("wowam.exe.new").exists())
    wowam.cleanup_after_update()
    check(".old removed on next start", not exe.with_name("wowam.exe.old").exists())

    for bad_repo, why in (("acme/badsum", "bad checksum"), ("acme/nosum", "missing checksum")):
        exe = fresh_exe()
        repo(bad_repo)
        bad = wowam.newer_release()
        try:
            wowam.apply_self_update(bad, log=lambda *_: None)
            check(f"{why} is refused", False)
        except wowam.WowamError:
            pass
        check(f"exe untouched after {why}", exe.read_bytes() == b"MZ-old-exe")
        check(f"no leftovers after {why}", sorted(p.name for p in exe.parent.iterdir()) == ["wowam.exe"])
finally:
    sys.executable = real_exe
    wowam.FROZEN = False

print("FAIL: " + "; ".join(problems) if problems else "ok")
sys.exit(1 if problems else 0)
