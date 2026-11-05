#!/usr/bin/env bash
cd "$(dirname "$0")"; ROOT="$(cd .. && pwd)"
set -u
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
export WOWAM_API_BASE=http://127.0.0.1:8765 WOWAM_API_KEY=TESTKEY WOWAM_GITHUB_API=http://127.0.0.1:8765
export XDG_CONFIG_HOME=$(mktemp -d); export WOWAM_CONFIG_DIR="$XDG_CONFIG_HOME/wowam"  # never touch the real config
W="python3 $ROOT/wowam.py"
G=$(mktemp -d); A="$G/World of Warcraft/_retail_/Interface/AddOns"; mkdir -p "$A"
# pre-existing addons: one tagged with a CurseForge id, one untagged, one Blizzard
mkdir -p "$A/OldAddon" "$A/MyLocalHack" "$A/Blizzard_Foo"
printf '## Title: Old\n## Version: 1.0\n## X-Curse-Project-ID: 400\n' > "$A/OldAddon/OldAddon.toc"
printf '## Title: Local\n' > "$A/MyLocalHack/MyLocalHack.toc"

python3 mock_cf.py 8765 & SRV=$!; sleep 1
trap 'kill $SRV 2>/dev/null' EXIT   # never leave the mock server running if the script aborts
pass=0; fail=0
ok(){ if eval "$2"; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1"; fail=$((fail+1)); fi; }

$W config --addons-dir "$A" >/dev/null
echo "--- scan";      $W scan
ok "scan adopts tagged addon"           'grep -q "\"400\"" "$A/../wowam-state.json"'
echo "--- install";   $W install https://www.curseforge.com/wow/addons/details
ok "install via URL"                    '[ -f "$A/Details/core.lua" ] && [ -d "$A/Details_Streamer" ]'
ok "dependency auto-installed"          '[ -d "$A/LibDep" ]'
ok "root README ignored"                '[ ! -e "$A/README.txt" ]'
echo "--- list";      $W list
echo "--- check";     $W check
ok "scanned addon flagged for sync"     '$W check | grep -q "Old Addon"'
echo "--- publish 1.1 + 1.2-beta"
curl -s -XPOST localhost:8765/admin/publish -d '{"mod":100,"file":1002}' >/dev/null
curl -s -XPOST localhost:8765/admin/publish -d '{"mod":100,"file":1003}' >/dev/null
echo "--- update";    $W update
ok "updated to latest release (not beta)" 'grep -q "1.1" "$A/Details/Details.toc"'
ok "stale folder from old version removed" '[ ! -d "$A/Details_Streamer" ]'
ok "backup of previous version kept"    '[ -d "$A/../wowam-backup/100/Details_Streamer" ]'
ok "scanned addon synced to real file"  'grep -q "9.9" "$A/OldAddon/OldAddon.toc"'
ok "untracked local addon untouched"    '[ -f "$A/MyLocalHack/MyLocalHack.toc" ]'
ok "second update is a no-op"           '$W update | grep -q "up to date"'
echo "--- beta channel"; $W install details --channel beta
ok "per-addon beta channel"             'grep -q "1.2b" "$A/Details/Details.toc"'
echo "--- pin";  $W pin "Old Addon"
ok "pinned excluded from check"         '! $W check | grep -q "Old Addon"'
echo "--- no-distribution addon"; $W install nodist 2>&1 | tee /tmp/nd.txt
ok "nodist reported clearly"            'grep -q "disabled third-party downloads" /tmp/nd.txt && [ ! -d "$A/NoDist" ]'
echo "--- zip-slip"; $W install evil 2>&1 | tee /tmp/ev.txt
ok "unsafe zip refused"                 'grep -q "unsafe path" /tmp/ev.txt && [ ! -e "$A/../escape.txt" ] && [ ! -d "$A/Evil" ]'
ok "no staging dirs left behind"        '[ -z "$(ls -A "$A/.." | grep "^.wowam-")" ]'
echo "--- manual install (author blocks downloads)"
ok "nodist message tells how to install by hand" 'grep -q "install-file" /tmp/nd.txt && grep -q "curseforge.com/wow/addons/nodist" /tmp/nd.txt'
ok "search flags manual-download addons" '$W search nodist | grep -q "manual"'
mkzip(){ python3 - "$1" "$2" "$3" <<'PY'
import sys, zipfile
out, ver, pid = sys.argv[1:4]
with zipfile.ZipFile(out, "w") as z:
    z.writestr("NoDist/NoDist.toc", f"## Title: NoDist\n## Version: {ver}\n" + (f"## X-Curse-Project-ID: {pid}\n" if pid else ""))
    z.writestr("NoDist/core.lua", f"-- {ver}\n")
PY
}
M=$(mktemp -d); mkzip "$M/3001.zip" 2.0 300
$W install-file "$M/3001.zip"
ok "install-file installs from local zip" 'grep -q "2.0" "$A/NoDist/NoDist.toc"'
ok "file matched to a CurseForge file (no nag)" '$W check | grep -q "up to date"'
ok "list shows it as manual" '$W list | grep NoDist | grep -q manual'
curl -s -XPOST localhost:8765/admin/publish -d '{"mod":300,"file":3002}' >/dev/null
$W check | tee /tmp/mc.txt
ok "check reports manual update + link" 'grep -q "NoDist" /tmp/mc.txt && grep -q "manual download" /tmp/mc.txt && grep -q "curseforge.com/wow/addons/nodist" /tmp/mc.txt'
$W update > /tmp/mu.txt; mu=$?; cat /tmp/mu.txt
ok "update does not fail on manual addon, leaves it alone" '[ $mu -eq 0 ] && grep -q "2.0" "$A/NoDist/NoDist.toc"'
mkzip "$M/3002.zip" 2.1 300; $W install-file "$M/3002.zip"
ok "install-file of new zip updates + backs up" 'grep -q "2.1" "$A/NoDist/NoDist.toc" && [ -d "$A/../wowam-backup/300" ]'
ok "up to date after manual update" '$W check | grep -q "up to date"'
mkzip "$M/renamed.zip" 2.0 300; $W install-file "$M/renamed.zip"
ok "unmatched zip name -> outdated detected via toc version" '$W check | grep -q "NoDist"'
mkzip "$M/other-name.zip" 2.1 300; $W install-file "$M/other-name.zip"
ok "unmatched zip name but current version -> no nag" '$W check | grep -q "up to date"'
mkzip "$M/untagged.zip" 1.0 ""; $W install-file "$M/untagged.zip" 2>&1 | tee /tmp/ut.txt
ok "untagged zip asks for --mod" 'grep -q -- "--mod" /tmp/ut.txt'
$W install-file "$M/untagged.zip" --mod nodist >/dev/null
ok "--mod override works" 'grep -q "1.0" "$A/NoDist/NoDist.toc"'
python3 - "$M/site-only.zip" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("NoDist/NoDist.toc", "## Title: NoDist\n## Version: 2.1\n## X-Website: https://www.curseforge.com/wow/addons/nodist\n")
PY
$W install-file "$M/site-only.zip" >/dev/null
ok "no project id: falls back to X-Website slug" 'grep -q "2.1" "$A/NoDist/NoDist.toc"'
echo "--- github source"
$W install --github acme/ghaddon
ok "github install" 'grep -q "v1.0" "$A/GhAddon/GhAddon.toc"'
ok "github listed as github" '$W list | grep -i ghaddon | grep -q github'
ok "github up to date" '! $W check | grep -qi "ghaddon"'
curl -s -XPOST localhost:8765/admin/gh_tag -d '{"repo":"acme/ghaddon","tag":"v1.1"}' >/dev/null
ok "github update detected" '$W check | grep -qi "ghaddon.*v1.1"'
$W update
ok "github update installed + backup (sanitized dir)" 'grep -q "v1.1" "$A/GhAddon/GhAddon.toc" && [ -d "$A/../wowam-backup/gh_acme_ghaddon" ]'
$W install --github acme/multizip 2>&1 | tee /tmp/mz.txt
ok "several zips -> asks for --asset" 'grep -q -- "--asset" /tmp/mz.txt'
$W install --github acme/nozip 2>&1 | tee /tmp/nz.txt
ok "release without zip explained" 'grep -q "no matching .zip" /tmp/nz.txt'
$W install https://github.com/acme/ghaddon >/dev/null
ok "github.com URL accepted by install" '$W list | grep -ci ghaddon | grep -q 1'
$W remove "gh:acme/ghaddon"
ok "github addon removable" '[ ! -d "$A/GhAddon" ]'
$W install --github acme/ghaddon >/dev/null
python3 - "$M/gh-as-cf.zip" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("GhAddon/GhAddon.toc", "## Title: GhAddon\n## Version: 9\n## X-Curse-Project-ID: 200\n")
PY
$W install-file "$M/gh-as-cf.zip" >/dev/null
ok "replaced github entry is dropped (no double tracking)" '! $W list | grep -q github && $W list | grep -q LibDep'

echo "--- failed swap rolls back"
ok "locked/failed folder swap restores previous version" 'python3 test_rollback.py | grep -q "^ok"'

echo "--- desktop shortcut"
SC=$(mktemp -d)
python3 -c "import sys; sys.path.insert(0, sys.argv[1]); import wowam; print(wowam.create_shortcut(wowam.Path(sys.argv[2])))" "$ROOT" "$SC"
ok "shortcut file created" '[ -n "$(ls "$SC" | grep -iE "wowam|wow addon manager")" ]'

echo "--- theme"
$W config --theme dark >/dev/null
ok "config --theme persists" '$W config | grep -q "\"theme\": \"dark\""'
ok "invalid theme rejected" '! $W config --theme neon 2>/dev/null'
ok "OS dark-mode probe returns a bool" 'python3 -c "import sys; sys.path.insert(0, sys.argv[1]); import wowam; print(wowam.system_prefers_dark())" "$ROOT" | grep -qE "^(True|False)$"'

echo "--- browse"
python3 test_browse.py | tee /tmp/br0.txt
ok "browse logic (sorts, paging, search-less key fallback)" 'grep -q "^ok" /tmp/br0.txt'
$W browse | tee /tmp/br1.txt
ok "browse lists the most popular first, ranked" 'grep -E "^1 +600 " /tmp/br1.txt | grep -q Questie && grep -q "Install one with" /tmp/br1.txt'
ok "browse flags manual-download addons" 'grep -E "NoDist" /tmp/br1.txt | grep -q manual'
$W browse --limit 2 --page 2 | tee /tmp/br2.txt
ok "browse paging continues the ranking" 'grep -E "^3 +400 " /tmp/br2.txt | grep -q "Old Addon" && ! grep -q Questie /tmp/br2.txt'
WOWAM_API_KEY=NOSEARCH $W browse | tee /tmp/br3.txt
ok "browse works for a key that can't search" 'grep -q Questie /tmp/br3.txt && grep -q "short featured" /tmp/br3.txt'
WOWAM_API_KEY=NOSEARCH $W search details 2>&1 | tee /tmp/br4.txt
ok "a key that can't browse either still finds featured addons by name" 'grep -q "Details" /tmp/br4.txt && grep -q "looked through" /tmp/br4.txt'
WOWAM_API_KEY=NOSEARCH $W install no-such-addon 2>&1 | tee /tmp/br5.txt
ok "unknown name explains what was scanned and what to do" 'grep -q "most popular addons I looked through" /tmp/br5.txt && grep -q "numeric project ID" /tmp/br5.txt && grep -q "wowam browse" /tmp/br5.txt'
echo "--- local search (key that can browse but not text-search)"
WOWAM_API_KEY=NOTEXT $W search details 2>&1 | tee /tmp/ls1.txt
ok "search finds addons by scanning the popular catalogue" 'grep -q "Details" /tmp/ls1.txt && grep -q "looked through" /tmp/ls1.txt'
WOWAM_API_KEY=NOTEXT $W install questie 2>&1 | tee /tmp/ls2.txt
ok "install by slug works for such a key" '[ -f "$A/Questie/core.lua" ]'
$W remove questie >/dev/null
WOWAM_API_KEY=NOTEXT $W install no-such-addon 2>&1 | tee /tmp/ls3.txt
ok "such a key gets a clear not-found message" 'grep -q "most popular addons I looked through" /tmp/ls3.txt'
ok "browse rejects an unknown sort" '! $W browse --sort nonsense 2>/dev/null'

echo "--- self-update"
python3 test_selfupdate.py | tee /tmp/su.txt
ok "self-update logic (version compare, throttle, checksum, swap)" 'grep -q "^ok" /tmp/su.txt'
WOWAM_UPDATE_REPO=acme/selfupd $W self-update --check | tee /tmp/su2.txt
ok "self-update --check reports the newer release" 'grep -q "9.9.9 is available" /tmp/su2.txt'
WOWAM_UPDATE_REPO=acme/selfold $W self-update --check | tee /tmp/su3.txt
ok "self-update --check says up to date" 'grep -q "latest version" /tmp/su3.txt'
WOWAM_UPDATE_REPO=acme/selfupd $W self-update | tee /tmp/su4.txt
ok "source install is pointed at the download page" 'grep -q "running from source" /tmp/su4.txt'

echo "--- remove"; $W remove libdep
ok "remove deletes folders"             '[ ! -d "$A/LibDep" ]'
echo "--- bad key"; WOWAM_API_KEY=nope $W check 2>&1 | tee /tmp/bk.txt
ok "bad key -> clear error"             'grep -q "403" /tmp/bk.txt'
echo "--- search"; $W search details
echo "--- flavors"; $W flavors
echo "--- detect (fake Lutris prefix)"
mkdir -p "$XDG_CONFIG_HOME/h/Games/battlenet/drive_c/Program Files (x86)/World of Warcraft/_retail_/Interface/AddOns"
HOME="$XDG_CONFIG_HOME/h" $W detect | tee /tmp/dt.txt
ok "detects Lutris install"             'grep -q "_retail_/Interface/AddOns" /tmp/dt.txt'

kill $SRV; echo; echo "$pass passed, $fail failed"
