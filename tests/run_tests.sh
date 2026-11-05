#!/usr/bin/env bash
cd "$(dirname "$0")"; ROOT="$(cd .. && pwd)"
set -u
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
export WOWAM_API_BASE=http://127.0.0.1:8765 WOWAM_API_KEY=TESTKEY
export XDG_CONFIG_HOME=$(mktemp -d)
W="python3 $ROOT/wowam.py"
G=$(mktemp -d); A="$G/World of Warcraft/_retail_/Interface/AddOns"; mkdir -p "$A"
# pre-existing addons: one tagged with a CurseForge id, one untagged, one Blizzard
mkdir -p "$A/OldAddon" "$A/MyLocalHack" "$A/Blizzard_Foo"
printf '## Title: Old\n## Version: 1.0\n## X-Curse-Project-ID: 400\n' > "$A/OldAddon/OldAddon.toc"
printf '## Title: Local\n' > "$A/MyLocalHack/MyLocalHack.toc"

python3 mock_cf.py 8765 & SRV=$!; sleep 1
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
