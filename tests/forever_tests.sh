export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
cd "$(dirname "$0")"; ROOT="$(cd .. && pwd)"
export WOWAM_API_BASE=http://127.0.0.1:8765 WOWAM_API_KEY=TESTKEY
export XDG_CONFIG_HOME=$(mktemp -d); H=$XDG_CONFIG_HOME/h
export WOWAM_CONFIG_DIR="$XDG_CONFIG_HOME/wowam"  # never touch the real config
W="python3 $ROOT/wowam.py"
WOW="$H/Games/battlenet/drive_c/Program Files (x86)/World of Warcraft"
mkdir -p "$WOW/_retail_/Interface/AddOns" "$WOW/_forever_/Interface/AddOns"; A="$WOW/_forever_/Interface/AddOns"
python3 mock_cf.py 8765 & SRV=$!; sleep 1
trap 'kill $SRV 2>/dev/null' EXIT   # never leave the mock server running if the script aborts
pass=0; fail=0; ok(){ if eval "$2"; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1"; fail=$((fail+1)); fi; }
echo "--- detect"; HOME=$H $W detect | tee /tmp/d.txt
ok "detect prefers _forever_"            'grep -q "Saved as your AddOns folder: .*_forever_" /tmp/d.txt'
$W config --addons-dir "$A" >/dev/null   # pin explicitly: detect may find a real install on this machine
echo "--- install questie"; $W install questie
ok "gets Forever file, not retail/beta"  'grep -q "forever" "$A/Questie/Questie.toc"'
ok "flavor id cached in config"          'grep -q 88888 $XDG_CONFIG_HOME/wowam/config.json'
echo "--- retail-only addon"; $W install details 2>&1 | tee /tmp/r.txt
ok "retail-only addon refused w/ hint"   'grep -q "no release-or-better file" /tmp/r.txt && [ ! -d "$A/Details" ]'
echo "--- search scoped"; $W search questie
ok "retail install unaffected"           '$W --addons-dir "$WOW/_retail_/Interface/AddOns" install details >/dev/null && [ -d "$WOW/_retail_/Interface/AddOns/Details" ]'
ok "explicit 'forever beta' resolves"    '$W --flavor "forever beta" check 2>&1 | grep -q 88889'
echo "--- wrong-flavor zip"
python3 - "$H/6001.zip" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("Questie/Questie.toc", "## Title: Questie" + chr(10) + "## Version: retail" + chr(10) + "## X-Curse-Project-ID: 600" + chr(10))
PY
$W install-file "$H/6001.zip" 2>&1 | tee /tmp/wf.txt
ok "retail zip refused in a Forever folder, names the right file" 'grep -q "different game version" /tmp/wf.txt && grep -q "6002.zip" /tmp/wf.txt && grep -q "forever" "$A/Questie/Questie.toc"'
kill $SRV; echo "$pass passed, $fail failed"
