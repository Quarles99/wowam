"""wowam_art: image codecs, the texture download/cache, addon icons and the frog. run_tests.sh starts the mock
server and sets WOWAM_API_BASE / WOWAM_ART_BASE; WOWAM_CONFIG_DIR keeps the caches out of the real config.
Prints "ok" when everything passes."""
import json
import os
import struct
import sys
import tempfile
import urllib.request
from pathlib import Path

if not os.environ.get("WOWAM_ART_BASE", "").startswith("http://127.0.0.1"):
    sys.exit("refusing to run without the local mock server (WOWAM_ART_BASE)")
HERE = Path(__file__).resolve().parent
os.environ["WOWAM_CONFIG_DIR"] = tempfile.mkdtemp()  # fresh caches: the GUI tests may have filled the shared one
sys.path.insert(0, str(HERE.parent))
import wowam  # noqa: E402
import wowam_art as A  # noqa: E402

problems = []


def check(name, cond):
    if not cond:
        problems.append(name)


def close(a, b, tol=8):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# --- PNG: our own encoder round-trips, and the decoder handles palette and grayscale PNGs too
img = A.Image(5, 3)
img.put(0, 0, (255, 0, 0, 255)); img.put(4, 2, (1, 2, 3, 128))
back = A.decode(img.to_png())
check("PNG round-trip", (back.w, back.h, bytes(back.px)) == (5, 3, bytes(img.px)))


def png_chunks(*chunks):
    out = b"\x89PNG\r\n\x1a\n"
    for t, d in chunks:
        out += struct.pack(">I", len(d)) + t + d + struct.pack(">I", A.zlib.crc32(t + d) & 0xFFFFFFFF)
    return out


pal = png_chunks((b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 3, 0, 0, 0)), (b"PLTE", bytes((10, 20, 30, 40, 50, 60))),
                 (b"tRNS", bytes((0,))), (b"IDAT", A.zlib.compress(b"\x00\x00\x01")), (b"IEND", b""))
p = A.decode(pal)
check("palette PNG with transparency", p.get(0, 0) == (10, 20, 30, 0) and p.get(1, 0) == (40, 50, 60, 255))
gray = png_chunks((b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 0, 0, 0, 0)), (b"IDAT", A.zlib.compress(b"\x01\x10\x20")),
                  (b"IEND", b""))
check("grayscale PNG with the Sub filter", A.decode(gray).get(1, 0) == (0x30, 0x30, 0x30, 255))

# --- JPEG: a baseline file (saved by Windows' own encoder) with four colored quadrants
j = A.decode((HERE / "logo.jpg").read_bytes())
check("JPEG size", (j.w, j.h) == (48, 32))
for (x, y), want in {(6, 4): (200, 30, 30), (40, 4): (30, 180, 40), (6, 28): (40, 60, 220),
                     (40, 28): (240, 220, 60)}.items():
    check(f"JPEG color at {x},{y}", close(j.get(x, y)[:3], want))
try:
    A.decode(b"\xff\xd8\xff\xc2\x00\x0b\x08\x00\x10\x00\x10\x01\x01\x11\x00")
    check("progressive JPEG refused", False)
except A.ImageError:
    pass

# --- TGA: RLE, bottom-up rows (the default), BGR order
tga = bytearray(18); tga[2] = 10; tga[12:18] = bytes((2, 0, 2, 0, 24, 0))
tga += bytes((0x81, 255, 0, 0))          # bottom row: 2 x blue (BGR 255,0,0)
tga += bytes((0x01, 0, 0, 255, 0, 255, 0))  # top row: red, green
t = A.decode(bytes(tga))
check("TGA RLE, bottom-up, BGR", t.get(0, 0)[:3] == (255, 0, 0) and t.get(1, 0)[:3] == (0, 255, 0)
      and t.get(0, 1)[:3] == (0, 0, 255))


# --- BLP2 (the game's texture format): DXT1, palette with 8-bit alpha, and raw BGRA
def blp(comp, adepth, atype, w, h, mip, palette=b""):
    head = b"BLP2" + struct.pack("<I", 1) + bytes((comp, adepth, atype, 0)) + struct.pack("<II", w, h)
    offs = [1172] + [0] * 15
    sizes = [len(mip)] + [0] * 15
    return head + struct.pack("<16I", *offs) + struct.pack("<16I", *sizes) + palette.ljust(1024, b"\0") + mip


dxt1 = struct.pack("<HHI", 0xF800, 0x001F, 0x55555555)  # every pixel uses color 1: blue
check("BLP DXT1", A.decode(blp(2, 0, 0, 4, 4, dxt1)).get(3, 3) == (0, 0, 255, 255))
pb = A.decode(blp(1, 8, 0, 2, 1, bytes((1, 0, 77, 200)), palette=bytes((0, 0, 0, 0, 30, 20, 10, 0))))
check("BLP palette + alpha", pb.get(0, 0) == (10, 20, 30, 77) and pb.get(1, 0) == (0, 0, 0, 200))
check("BLP BGRA", A.decode(blp(3, 8, 0, 1, 1, bytes((1, 2, 3, 4)))).get(0, 0) == (3, 2, 1, 4))

# --- hostile or unknown files fail cleanly
for bad in (b"", b"GIF89a....", b"\x89PNG\r\n\x1a\n" + b"\0" * 20, b"BLP2" + b"\xff" * 2000, b"not an image at all"):
    try:
        A.decode(bad)
        check(f"garbage refused: {bad[:8]!r}", False)
    except (A.ImageError, ValueError, IndexError, struct.error, A.zlib.error):
        pass
try:
    A.Image(5000, 5000)
    check("huge image refused", False)
except A.ImageError:
    pass

# --- scale and frame keep transparency from bleeding
sq = A._square(A.Image(40, 30, fill=(0, 200, 0, 255)), 64)
check("square crop + scale", (sq.w, sq.h) == (64, 64) and sq.get(10, 10) == (0, 200, 0, 255))
f = A.framed(sq, 24)
check("framed icon has a dark 1px frame", f.get(0, 0)[:3] == (0, 0, 0) and f.get(12, 12)[:3] == (0, 200, 0))

# --- the classic textures: downloaded once from the mock, then served from the cache
hits = lambda: json.loads(urllib.request.urlopen(os.environ["WOWAM_API_BASE"] + "/admin/art_hits").read())
before = len(hits())
check("art downloads", A.ensure_art())
n = len(hits()) - before
check("every texture fetched", n == len(A.ART_FILES))
check("second run uses the cache", A.ensure_art() and len(hits()) - before == n)
art = A.load_art()
kit = A.build_kit(art, button_h=24)
check("theme kit", kit["button"].h == 24 and kit["button"].w == 28 and kit["panel"].w == 288 and kit["entry_field"].startswith("#"))
rimmed = A.Image(10, 8, fill=(150, 10, 10, 176))  # a solid button inside a semi-transparent red rim
rimmed.over(A.Image(6, 4, fill=(90, 0, 0, 255)), 2, 2)
cut = A.bezel_mask(rimmed)(rimmed)
check("button rim cut off (it showed as red past the bezel)", (cut.w, cut.h) == (6, 4)
      and all(v == 255 for v in cut.px[3::4]))
check("textures for window, list and log", all(k in kit for k in ("bg", "list", "parchment", "sel")))
check("plaque widens", A.plaque(kit, 300).w == 300)
bad_base = os.environ["WOWAM_ART_BASE"]
os.environ["WOWAM_ART_BASE"] = bad_base + "Missing/"
for p in A.art_dir().glob("*.png"):
    p.unlink()
logged = []
check("offline/blocked art fails softly", A.ensure_art(log=logged.append) is False and logged)
os.environ["WOWAM_ART_BASE"] = bad_base

# --- addon icons
store = A.IconStore()
check("CurseForge PNG logo", store.fetch("100", logo_url=os.environ["WOWAM_API_BASE"] + "/logo/100.png") is not None)
check("cached as a 64px PNG", store.cached("100") is not None and store.cached("100").w == 64)
check("CurseForge JPEG logo", store.fetch("600", logo_url=os.environ["WOWAM_API_BASE"] + "/logo/600.jpg") is not None)
check("broken logo link: no icon, retried later", store.fetch("400", logo_url=os.environ["WOWAM_API_BASE"]
                                                              + "/logo/missing.png") is None
      and not store.recently_missed("400"))
check("addon with no icon is remembered", store.fetch("200") is None and store.recently_missed("200"))
check("gh: keys get a safe file name", store._file("gh:acme/x").parent == store.dir and "/" not in
      store._file("gh:acme/x").name.replace(os.sep, ""))

add = Path(tempfile.mkdtemp()) / "AddOns"
(add / "Iconic" / "media").mkdir(parents=True)
(add / "Iconic" / "media" / "Logo.TGA").write_bytes(bytes(tga))
(add / "Iconic" / "Iconic.toc").write_text("## Title: Iconic\n## IconTexture: Interface\\AddOns\\Iconic\\media\\logo\n")
check(".toc IconTexture (TGA, case-insensitive, no extension)", A.toc_icon(add, ["Iconic"]) is not None)
(add / "Sneaky").mkdir()
(add.parent / "secret.tga").write_bytes(bytes(tga))
(add / "Sneaky" / "Sneaky.toc").write_text("## IconTexture: ..\\..\\secret\n")
check(".toc pointing outside AddOns is ignored", A.toc_icon(add, ["Sneaky"]) is None)
check("toc icon wins over the logo", store.fetch("iconic", add, ["Iconic"], "http://127.0.0.1:1/x.png") is not None)

# --- the WoW icon border: Blizzard's icons already have it, anything else gets the action-button frame
bevel = A.Image(64, 64, fill=(120, 60, 20, 255))
for d, c in ((0, 0), (1, 90), (2, 110), (3, 8)):
    for i in range(d, 64 - d):
        for x, y in ((i, d), (i, 63 - d), (d, i), (63 - d, i)):
            bevel.put(x, y, (c, c, c, 255))
check("baked-in icon border detected", A.has_icon_border(bevel))
check("full-bleed logo has no border", not A.has_icon_border(sq) and not A.has_icon_border(A._square(j, 64)))
slot = A.Image(39, 39)
for i in range(39):
    for x, y in ((i, 0), (i, 38), (0, i), (38, i)):
        slot.put(x, y, (200, 200, 200, 255))
added = A.framed(sq, 28, slot)
check("frame added when missing", added.get(0, 14)[:3] == (200, 200, 200) and added.get(14, 14)[:3] == (0, 200, 0))
kept = A.framed(bevel, 28, slot)
check("existing border kept (no second frame)", kept.get(0, 14)[:3] != (200, 200, 200))
check("kit carries the slot frame", kit["slot"].w > 0)


# --- the game's font: family name read from the file; never downloaded, only found on disk
def ttf(family):
    name = family.encode("utf-16-be")
    table = struct.pack(">HHH", 0, 1, 18) + struct.pack(">HHHHHH", 3, 1, 0x409, 1, len(name), 0) + name
    return struct.pack(">IHHHH", 0x10000, 1, 0, 0, 0) + struct.pack(">4sIII", b"name", 0, 28, len(table)) + table


fake = Path(tempfile.mkdtemp())
(fake / "f.ttf").write_bytes(ttf("Friz Quadrata TT"))
check("TTF family name", A.ttf_family(fake / "f.ttf") == "Friz Quadrata TT")
check("not a font", A.ttf_family(HERE / "logo.jpg") is None)
wow = Path(tempfile.mkdtemp()) / "World of Warcraft"
(wow / "_classic_" / "Fonts").mkdir(parents=True)
(wow / "_forever_" / "Interface" / "AddOns").mkdir(parents=True)
(wow / "_classic_" / "Fonts" / "FRIZQT__.TTF").write_bytes(ttf("Friz Quadrata TT"))
check("font found in a sibling WoW folder", A.find_wow_font(wow / "_forever_" / "Interface" / "AddOns") is not None)
check("no font, no guess", A.find_wow_font(fake / "nope") is None)

# --- the frog
check("frog sprites", A.frog().w == 32 and A.frog(leap=True).h == A.frog().h and A.frog(scale=1).w == 16)

print("ok" if not problems else "FAILED: " + "; ".join(problems))
