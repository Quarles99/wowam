"""Minimal fake CurseForge API for end-to-end testing wowam."""
import io, json, re, sys, threading, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
BASE = f"http://127.0.0.1:{PORT}"
RETAIL = 517


def zip_of(folders, evil=False):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for folder, ver in folders:
            z.writestr(f"{folder}/{folder}.toc", f"## Title: {folder}\n## Version: {ver}\n")
            z.writestr(f"{folder}/core.lua", f"-- {folder} {ver}\n")
        z.writestr("README.txt", "root file, should be ignored")
        if evil:
            z.writestr("../escape.txt", "pwned")
    return b.getvalue()


FILES = {
    1001: dict(modId=100, displayName="Details 1.0", releaseType=1, folders=[("Details", "1.0"), ("Details_Streamer", "1.0")], deps=[200]),
    1002: dict(modId=100, displayName="Details 1.1", releaseType=1, folders=[("Details", "1.1")], deps=[200]),
    1003: dict(modId=100, displayName="Details 1.2-beta", releaseType=2, folders=[("Details", "1.2b")], deps=[200]),
    2001: dict(modId=200, displayName="LibDep r5", releaseType=1, folders=[("LibDep", "r5")], deps=[]),
    3001: dict(modId=300, displayName="NoDist 2.0", releaseType=1, folders=[("NoDist", "2.0")], deps=[], nodist=True),
    4001: dict(modId=400, displayName="OldAddon 9.9", releaseType=1, folders=[("OldAddon", "9.9")], deps=[]),
    6001: dict(modId=600, displayName="Questie retail", releaseType=1, folders=[("Questie", "retail")], deps=[]),
    6002: dict(modId=600, displayName="Questie forever", releaseType=1, folders=[("Questie", "forever")], deps=[], flavor=88888),
    5001: dict(modId=500, displayName="Evil 1", releaseType=1, folders=[("Evil", "1")], deps=[], evil=True),
}
MODS = {100: ("Details! Damage Meter", "details"), 200: ("LibDep", "libdep"),
        300: ("NoDist", "nodist"), 400: ("Old Addon", "old-addon"), 500: ("Evil", "evil"), 600: ("Questie", "questie")}
LIVE = {100: [1001], 200: [2001], 300: [3001], 400: [4001], 500: [5001], 600: [6001, 6002]}  # visible files per mod


def file_obj(fid):
    f = FILES[fid]
    return {"id": fid, "modId": f["modId"], "displayName": f["displayName"], "fileName": f"{fid}.zip",
            "releaseType": f["releaseType"], "fileDate": "2026-11-01T00:00:00Z", "isAvailable": True,
            "downloadUrl": None if f.get("nodist") else f"{BASE}/dl/{fid}.zip",
            "dependencies": [{"modId": d, "relationType": 3} for d in f["deps"]],
            "gameVersions": ["11.2.5"],
            "sortableGameVersions": [{"gameVersionTypeId": f.get("flavor", RETAIL), "gameVersion": "x"}]}


def mod_obj(mid):
    name, slug = MODS[mid]
    return {"id": mid, "gameId": 1, "name": name, "slug": slug, "summary": f"{name} summary",
            "downloadCount": mid * 1000, "links": {"websiteUrl": f"https://www.curseforge.com/wow/addons/{slug}"},
            "latestFilesIndexes": [{"fileId": fid, "gameVersionTypeId": FILES[fid].get("flavor", RETAIL), "releaseType": FILES[fid]["releaseType"],
                                    "gameVersion": "11.2.5", "filename": f"{fid}.zip"} for fid in LIVE[mid]]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, obj, code=200, raw=None, ctype="application/json"):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def authed(self):
        if self.path.startswith("/v1") and self.headers.get("x-api-key") != "TESTKEY":
            self.send({"error": "forbidden"}, 403); return False
        return True

    def do_GET(self):
        if not self.authed(): return
        p, _, qs = self.path.partition("?")
        q = dict(kv.split("=", 1) for kv in qs.split("&") if "=" in kv)
        if p == "/v1/games/1/version-types":
            return self.send({"data": [{"id": 517, "name": "WoW Retail", "slug": "wow_retail"},
                                       {"id": 67408, "name": "WoW Classic", "slug": "wow_classic"},
                                       {"id": 88889, "name": "WoW Forever Beta", "slug": "wow_forever_beta"},
                                       {"id": 88888, "name": "WoW Forever", "slug": "wow_forever"}]})
        if p == "/v1/mods/search":
            from urllib.parse import unquote_plus
            term = unquote_plus(q.get("searchFilter", "")).lower(); slug = q.get("slug")
            hits = [mod_obj(m) for m, (n, s) in MODS.items() if (slug and s == slug) or (term and term in n.lower())]
            return self.send({"data": hits})
        m = re.fullmatch(r"/v1/mods/(\d+)", p)
        if m: return self.send({"data": mod_obj(int(m[1]))})
        m = re.fullmatch(r"/v1/mods/(\d+)/files", p)
        if m: return self.send({"data": [file_obj(f) for f in LIVE[int(m[1])]]})
        m = re.fullmatch(r"/v1/mods/(\d+)/files/(\d+)/download-url", p)
        if m:
            f = FILES[int(m[2])]
            return self.send({"data": None if f.get("nodist") else f"{BASE}/dl/{m[2]}.zip"})
        m = re.fullmatch(r"/dl/(\d+)\.zip", p)
        if m:
            f = FILES[int(m[1])]
            return self.send(None, raw=zip_of(f["folders"], f.get("evil")), ctype="application/zip")
        self.send({"error": "nf"}, 404)

    def do_POST(self):
        if not self.authed(): return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/v1/mods":
            return self.send({"data": [mod_obj(i) for i in body["modIds"] if i in MODS]})
        if self.path == "/v1/mods/files":
            return self.send({"data": [file_obj(i) for i in body["fileIds"] if i in FILES]})
        if self.path == "/admin/publish":  # test hook: make a new file live
            LIVE[body["mod"]].append(body["file"]); return self.send({"ok": True})
        self.send({"error": "nf"}, 404)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
