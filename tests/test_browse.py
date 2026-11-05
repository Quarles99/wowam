"""CurseForge.browse against the mock API (stdlib only). run_tests.sh starts the mock server and sets
WOWAM_API_BASE; the NOSEARCH key mimics a real key that can't use /mods/search."""
import os
import sys
from pathlib import Path

if not os.environ.get("WOWAM_API_BASE", "").startswith("http://127.0.0.1"):
    sys.exit("refusing to run without the local mock server (WOWAM_API_BASE)")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import wowam

problems = []


def check(name, cond):
    if not cond:
        problems.append(name)


def names(r):
    return [m["name"] for m in r["mods"]]


full = wowam.CurseForge("TESTKEY")
p1 = full.browse("popular", 517, 2, 0)
check("popular page 1 is the two most downloaded", names(p1) == ["Questie", "Evil"])
check("page 1 says there is more", p1["more"] and not p1["limited"])
p2 = full.browse("popular", 517, 2, 2)
check("popular page 2 continues the ranking", names(p2) == ["Old Addon", "NoDist"])
last = full.browse("popular", 517, 2, 4)
check("last page says no more", names(last) == ["LibDep", "Details! Damage Meter"] and not last["more"])
check("other sorts accepted", all(full.browse(s, 517, 3, 0)["mods"] for s in ("updated", "downloads")))
try:
    full.browse("nonsense", 517)
    check("unknown sort rejected", False)
except wowam.WowamError:
    pass

limited = wowam.CurseForge("NOSEARCH")
r = limited.browse("popular", 517, 25, 0)
check("search-less key falls back to featured lists", r["limited"] and r["mods"])
check("fallback has no duplicates", len({m["id"] for m in r["mods"]}) == len(r["mods"]))
check("fallback paging slices the list", limited.browse("popular", 517, 2, 0)["more"] and
      len(limited.browse("popular", 517, 2, 0)["mods"]) == 2)
d = limited.browse("downloads", 517, 25, 0)["mods"]
check("fallback 'downloads' is sorted by download count",
      [m["downloadCount"] for m in d] == sorted((m["downloadCount"] for m in d), reverse=True))
check("fallback 'updated' uses the recently-updated list",
      {m["id"] for m in limited.browse("updated", 517, 25, 0)["mods"]} != {m["id"] for m in r["mods"]})
check("fallback past the end is empty", limited.browse("popular", 517, 25, 500) == {"mods": [], "more": False, "limited": True})
try:
    limited.search("details")
    check("search-less key gets a clear search error", False)
except wowam.SearchForbidden:
    pass

# --- local search (search_any): used when text search is refused but browsing works (Will's real key)
real = full.search_any("details", 517)
check("a key that can search uses the real search", not real["local"] and names(real) == ["Details! Damage Meter"])

nt = wowam.CurseForge("NOTEXT")
r = nt.search_any("details", 517)
check("text-less key: found by local scan", r["local"] and names(r) == ["Details! Damage Meter"])
check("scan covered the whole small catalogue", r["scanned"] == 6 and not r["truncated"] and not r["limited"])
check("local match is case-insensitive", names(nt.search_any("DETAILS", 517)) == ["Details! Damage Meter"])
check("every word must match", names(nt.search_any("old addon", 517)) == ["Old Addon"])
check("slug text matches too", names(nt.search_any("old-addon", 517)) == ["Old Addon"])
check("exact slug lookup", names(nt.search_any(slug="nodist", flavor=517)) == ["NoDist"])
check("slug is exact, not a substring", nt.search_any(slug="nod", flavor=517)["mods"] == [])
miss = nt.search_any("zzz-nothing", 517)
check("no match is an empty, complete result", miss["mods"] == [] and miss["scanned"] == 6 and not miss["truncated"])

cut = nt.search_any("details", 517, scan_pages=1, scan_page_size=2)   # catalogue in pages of 2, look at one
check("stopping early is flagged truncated", cut["mods"] == [] and cut["truncated"] and cut["scanned"] == 2)
more = nt.search_any("details", 517, scan_pages=3, scan_page_size=2)
check("scanning further pages finds it", names(more) == ["Details! Damage Meter"] and not more["truncated"])
early = nt.search_any(slug="questie", flavor=517, scan_pages=10, scan_page_size=2)
check("slug lookup stops at the first hit", names(early) == ["Questie"] and early["scanned"] == 2 and not early["truncated"])

fresh = wowam.CurseForge("NOTEXT")
calls = []
orig = fresh._request
fresh._request = lambda path, params=None, **k: (calls.append((path, params)), orig(path, params, **k))[1]
fresh.search_any("details", 517)
pages_fetched = sum(1 for _, p in calls if p and "index" in p)
fresh.search_any("questie", 517)
check("catalogue pages are cached between lookups", sum(1 for _, p in calls if p and "index" in p) == pages_fetched)

ns = wowam.CurseForge("NOSEARCH").search_any("questie", 517)
check("a key that can't even browse scans the featured lists", ns["local"] and ns["limited"] and names(ns) == ["Questie"])

print("FAIL: " + "; ".join(problems) if problems else "ok")
sys.exit(1 if problems else 0)
