"""Artwork for the GUI's Warcraft theme, standard library only.

- A small image toolkit: decode PNG, baseline JPEG, TGA and BLP2 (WoW's texture format); encode PNG;
  crop, rotate, scale, blend. Everything is pure Python and size-limited, so a hostile file can only fail.
- The classic WoW UI textures, downloaded once from a commit-pinned mirror and cached in the config dir.
  They are Blizzard's art, so wowam never ships them in the repo or the exe: each user's copy fetches them.
- Addon icons: the .toc IconTexture (what the in-game addon list shows) or the CurseForge logo, cached.
- The frog. (Our own pixel art.)
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import struct
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import wowam

MAX_PIXELS = 2048 * 2048
MAX_DOWNLOAD = 4 * 1024 * 1024


class ImageError(Exception):
    pass


# =========================================================================== image

class Image:
    """RGBA, 8 bits per channel, rows top to bottom. Alpha is straight (not premultiplied)."""
    __slots__ = ("w", "h", "px")

    def __init__(self, w: int, h: int, px=None, fill=(0, 0, 0, 0)):
        if w <= 0 or h <= 0 or w * h > MAX_PIXELS:
            raise ImageError(f"bad image size {w}x{h}")
        self.w, self.h = w, h
        self.px = bytearray(px) if px is not None else bytearray(bytes(fill) * (w * h))
        if len(self.px) != w * h * 4:
            raise ImageError("pixel data has the wrong length")

    def get(self, x, y):
        i = (y * self.w + x) * 4
        return tuple(self.px[i:i + 4])

    def put(self, x, y, rgba):
        i = (y * self.w + x) * 4
        self.px[i:i + 4] = bytes(rgba)

    def copy(self) -> "Image":
        return Image(self.w, self.h, self.px)

    def crop(self, x, y, w, h) -> "Image":
        x, y = max(0, x), max(0, y)
        w, h = min(w, self.w - x), min(h, self.h - y)
        out = bytearray()
        for row in range(y, y + h):
            i = (row * self.w + x) * 4
            out += self.px[i:i + w * 4]
        return Image(w, h, out)

    def crop_frac(self, l, r, t, b) -> "Image":
        """Crop by texture coordinates (0..1), the way WoW's SetTexCoord addresses a texture."""
        x0, x1 = round(l * self.w), round(r * self.w)
        y0, y1 = round(t * self.h), round(b * self.h)
        return self.crop(x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    def rotate(self, cw: bool = True) -> "Image":
        out = Image(self.h, self.w)
        for y in range(self.h):
            for x in range(self.w):
                i = (y * self.w + x) * 4
                nx, ny = (self.h - 1 - y, x) if cw else (y, self.w - 1 - x)
                j = (ny * out.w + nx) * 4
                out.px[j:j + 4] = self.px[i:i + 4]
        return out

    def flip_h(self) -> "Image":
        out = bytearray()
        for y in range(self.h):
            row = self.px[y * self.w * 4:(y + 1) * self.w * 4]
            for x in range(self.w - 1, -1, -1):
                out += row[x * 4:x * 4 + 4]
        return Image(self.w, self.h, out)

    def scale(self, w: int, h: int) -> "Image":
        """Area-average resample (good for shrinking icons; blocky-but-clean when enlarging)."""
        if (w, h) == (self.w, self.h):
            return self.copy()
        # premultiply so transparent pixels don't bleed their color into the edges
        src = self.px
        pre = [0.0] * (self.w * self.h * 4)
        for i in range(0, len(src), 4):
            a = src[i + 3] / 255.0
            pre[i], pre[i + 1], pre[i + 2], pre[i + 3] = src[i] * a, src[i + 1] * a, src[i + 2] * a, float(src[i + 3])
        xs, ys = _weights(self.w, w), _weights(self.h, h)
        # horizontal pass
        mid = [0.0] * (w * self.h * 4)
        for y in range(self.h):
            base = y * self.w * 4
            for x, contrib in enumerate(xs):
                r = g = b = a = 0.0
                for sx, wt in contrib:
                    i = base + sx * 4
                    r += pre[i] * wt; g += pre[i + 1] * wt; b += pre[i + 2] * wt; a += pre[i + 3] * wt
                j = (y * w + x) * 4
                mid[j], mid[j + 1], mid[j + 2], mid[j + 3] = r, g, b, a
        out = bytearray(w * h * 4)
        for y, contrib in enumerate(ys):
            for x in range(w):
                r = g = b = a = 0.0
                for sy, wt in contrib:
                    i = (sy * w + x) * 4
                    r += mid[i] * wt; g += mid[i + 1] * wt; b += mid[i + 2] * wt; a += mid[i + 3] * wt
                j = (y * w + x) * 4
                if a > 0.5:
                    k = 255.0 / a
                    out[j], out[j + 1], out[j + 2] = _c(r * k), _c(g * k), _c(b * k)
                out[j + 3] = _c(a)
        return Image(w, h, out)

    def over(self, src: "Image", x: int = 0, y: int = 0, opacity: float = 1.0) -> "Image":
        """Alpha-composite src onto self at (x, y), in place."""
        for sy in range(src.h):
            dy = y + sy
            if not 0 <= dy < self.h:
                continue
            for sx in range(src.w):
                dx = x + sx
                if not 0 <= dx < self.w:
                    continue
                i = (sy * src.w + sx) * 4
                sa = src.px[i + 3] * opacity / 255.0
                if sa <= 0:
                    continue
                j = (dy * self.w + dx) * 4
                da = self.px[j + 3] / 255.0
                oa = sa + da * (1 - sa)
                for c in range(3):
                    self.px[j + c] = _c((src.px[i + c] * sa + self.px[j + c] * da * (1 - sa)) / oa)
                self.px[j + 3] = _c(oa * 255)
        return self

    def add(self, src: "Image", x: int = 0, y: int = 0, opacity: float = 1.0) -> "Image":
        """Additive blend (WoW's "ADD" blend mode, used by button highlights), in place."""
        for sy in range(src.h):
            dy = y + sy
            if not 0 <= dy < self.h:
                continue
            for sx in range(src.w):
                dx = x + sx
                if not 0 <= dx < self.w:
                    continue
                i = (sy * src.w + sx) * 4
                k = src.px[i + 3] * opacity / 255.0
                j = (dy * self.w + dx) * 4
                for c in range(3):
                    self.px[j + c] = _c(self.px[j + c] + src.px[i + c] * k)
        return self

    def tile_into(self, dst: "Image", x: int, y: int, w: int, h: int) -> None:
        for ty in range(y, y + h, self.h):
            for tx in range(x, x + w, self.w):
                part = self.crop(0, 0, min(self.w, x + w - tx), min(self.h, y + h - ty))
                dst.over(part, tx, ty)

    def flatten(self, bg) -> "Image":
        """Composite onto a solid color: Tk has no real alpha blending against what is behind a widget."""
        out = Image(self.w, self.h, fill=(bg[0], bg[1], bg[2], 255))
        return out.over(self)

    def average(self):
        n = r = g = b = 0
        for i in range(0, len(self.px), 4):
            a = self.px[i + 3]
            r += self.px[i] * a; g += self.px[i + 1] * a; b += self.px[i + 2] * a; n += a
        return (r // n, g // n, b // n) if n else (0, 0, 0)

    def bbox(self, threshold: int = 8):
        """Bounding box (x, y, w, h) of the pixels more opaque than threshold, or None."""
        xs, ys = [], []
        for y in range(self.h):
            for x in range(self.w):
                if self.px[(y * self.w + x) * 4 + 3] > threshold:
                    xs.append(x); ys.append(y)
        return (min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1) if xs else None

    def to_png(self) -> bytes:
        raw = bytearray()
        stride = self.w * 4
        for y in range(self.h):
            raw.append(0)
            raw += self.px[y * stride:(y + 1) * stride]

        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))

    def tk_data(self) -> str:
        """What tk.PhotoImage(data=...) takes (Tk 8.6 reads base64 PNG)."""
        return base64.b64encode(self.to_png()).decode("ascii")


def _c(v) -> int:
    return 0 if v <= 0 else 255 if v >= 255 else int(v + 0.5)


def _weights(n_src: int, n_dst: int):
    """For each destination index, the (source index, weight) pairs of a box filter."""
    out = []
    scale = n_src / n_dst
    for d in range(n_dst):
        lo, hi = d * scale, (d + 1) * scale
        if scale < 1:  # enlarging: nearest sample
            out.append([(min(n_src - 1, int((lo + hi) / 2)), 1.0)])
            continue
        contrib = []
        s = int(lo)
        while s < hi and s < n_src:
            cover = min(hi, s + 1) - max(lo, s)
            if cover > 0:
                contrib.append((s, cover / scale))
            s += 1
        out.append(contrib)
    return out


def decode(data: bytes) -> Image:
    """Any supported format, sniffed from its bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return decode_png(data)
    if data[:2] == b"\xff\xd8":
        return decode_jpeg(data)
    if data[:4] == b"BLP2":
        return decode_blp(data)
    if data[:4] in (b"BLP1", b"BLP0"):
        raise ImageError("old BLP1 textures aren't supported")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        raise ImageError("GIF isn't supported")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        raise ImageError("WebP isn't supported")
    return decode_tga(data)  # TGA has no magic number; its header is validated instead


# --------------------------------------------------------------------------- PNG

def decode_png(data: bytes) -> Image:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ImageError("not a PNG")
    pos, idat, plte, trns, hdr = 8, [], None, None, None
    while pos + 8 <= len(data):
        ln, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", body[:13])
        elif typ == b"PLTE":
            plte = body
        elif typ == b"tRNS":
            trns = body
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
    if not hdr:
        raise ImageError("PNG without a header")
    w, h, depth, ctype, _, _, interlace = hdr
    if w * h > MAX_PIXELS or not w or not h:
        raise ImageError("PNG too large")
    if interlace:
        raise ImageError("interlaced PNG isn't supported")
    chans = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if chans is None or depth not in (1, 2, 4, 8, 16) or (depth < 8 and ctype not in (0, 3)):
        raise ImageError("unsupported PNG format")
    bits_pp = chans * depth
    stride = (w * bits_pp + 7) // 8
    bpp = max(1, bits_pp // 8)
    d = zlib.decompressobj()
    raw = d.decompress(b"".join(idat), (stride + 1) * h + 1)
    if len(raw) < (stride + 1) * h:
        raise ImageError("PNG data is truncated")
    rows, prev = [], bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        elif f != 0:
            raise ImageError("bad PNG filter")
        rows.append(line)
        prev = line
    out = bytearray(w * h * 4)
    o = 0
    tr = None
    if trns and ctype == 0 and len(trns) >= 2:
        tr = struct.unpack(">H", trns[:2])[0]
    elif trns and ctype == 2 and len(trns) >= 6:
        tr = struct.unpack(">HHH", trns[:6])
    for line in rows:
        if depth == 16:
            line = line[0::2]  # keep the high byte of each sample
        if depth < 8:
            mask, per = (1 << depth) - 1, 8 // depth
            samples = [(line[x // per] >> (8 - depth * (x % per + 1))) & mask for x in range(w)]
            if ctype == 0:
                k = 255 // mask
                for v in samples:
                    a = 0 if tr is not None and v == tr else 255
                    out[o:o + 4] = bytes((v * k, v * k, v * k, a)); o += 4
                continue
            line = bytearray(samples)
        if ctype == 6:
            out[o:o + w * 4] = line[:w * 4]; o += w * 4
        elif ctype == 2:
            for x in range(w):
                r, g, b = line[x * 3:x * 3 + 3]
                a = 0 if tr is not None and depth == 8 and (r, g, b) == tr else 255
                out[o:o + 4] = bytes((r, g, b, a)); o += 4
        elif ctype == 4:
            for x in range(w):
                v, a = line[x * 2], line[x * 2 + 1]
                out[o:o + 4] = bytes((v, v, v, a)); o += 4
        elif ctype == 0:
            for v in line[:w]:
                a = 0 if tr is not None and depth == 8 and v == tr else 255
                out[o:o + 4] = bytes((v, v, v, a)); o += 4
        else:  # palette
            if not plte:
                raise ImageError("palette PNG without a palette")
            for v in line[:w]:
                if v * 3 + 3 > len(plte):
                    raise ImageError("bad palette index")
                a = trns[v] if trns and v < len(trns) else 255
                out[o:o + 4] = bytes((plte[v * 3], plte[v * 3 + 1], plte[v * 3 + 2], a)); o += 4
    return Image(w, h, out)


# --------------------------------------------------------------------------- JPEG (baseline)

_ZIGZAG = [0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5, 12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6,
           7, 14, 21, 28, 35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51, 58, 59, 52, 45, 38, 31,
           39, 46, 53, 60, 61, 54, 47, 55, 62, 63]
_IDCT = [[(math.sqrt(0.5) if u == 0 else 1.0) / 2 * math.cos((2 * x + 1) * u * math.pi / 16) for u in range(8)]
         for x in range(8)]


class _Huff:
    def __init__(self, counts, symbols):
        self.maxcode, self.valptr, self.mincode, self.symbols = [-1] * 17, [0] * 17, [0] * 17, symbols
        code = k = 0
        for length in range(1, 17):
            n = counts[length - 1]
            if n:
                self.valptr[length], self.mincode[length] = k, code
                code += n
                k += n
                self.maxcode[length] = code - 1
            code <<= 1


class _Bits:
    def __init__(self, data):
        self.d, self.i, self.acc, self.n = data, 0, 0, 0

    def bit(self):
        if not self.n:
            self.acc = self.d[self.i] if self.i < len(self.d) else 0
            self.i += 1
            self.n = 8
        self.n -= 1
        return (self.acc >> self.n) & 1

    def get(self, k):
        v = 0
        for _ in range(k):
            v = (v << 1) | self.bit()
        return v

    def decode(self, t: _Huff):
        code = 0
        for length in range(1, 17):
            code = (code << 1) | self.bit()
            if code <= t.maxcode[length]:
                return t.symbols[t.valptr[length] + code - t.mincode[length]]
        raise ImageError("bad JPEG Huffman code")


def _extend(v, s):
    return v - (1 << s) + 1 if s and v < (1 << (s - 1)) else v


def decode_jpeg(data: bytes) -> Image:
    if data[:2] != b"\xff\xd8":
        raise ImageError("not a JPEG")
    pos, qt, dc_t, ac_t, frame, restart, transform = 2, {}, {}, {}, None, 0, None
    while pos < len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        pos += 2
        if marker in (0xD8, 0x01, 0xFF) or 0xD0 <= marker <= 0xD7:
            if marker == 0xFF:
                pos -= 1
            continue
        if marker == 0xD9:
            break
        ln = struct.unpack(">H", data[pos:pos + 2])[0]
        seg = data[pos + 2:pos + ln]
        pos += ln
        if marker == 0xDB:
            i = 0
            while i < len(seg):
                pq, tq = seg[i] >> 4, seg[i] & 15
                i += 1
                if pq:
                    qt[tq] = list(struct.unpack(">64H", seg[i:i + 128])); i += 128
                else:
                    qt[tq] = list(seg[i:i + 64]); i += 64
        elif marker == 0xC4:
            i = 0
            while i < len(seg):
                tc, th = seg[i] >> 4, seg[i] & 15
                counts = seg[i + 1:i + 17]
                n = sum(counts)
                (ac_t if tc else dc_t)[th] = _Huff(counts, list(seg[i + 17:i + 17 + n]))
                i += 17 + n
        elif marker in (0xC0, 0xC1):
            h, w = struct.unpack(">HH", seg[1:5])
            comps = [dict(id=seg[6 + k * 3], h=seg[7 + k * 3] >> 4, v=seg[7 + k * 3] & 15, tq=seg[8 + k * 3])
                     for k in range(seg[5])]
            frame = (w, h, comps)
        elif 0xC2 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            raise ImageError("progressive or other non-baseline JPEG isn't supported")
        elif marker == 0xDD:
            restart = struct.unpack(">H", seg[:2])[0]
        elif marker == 0xEE and seg[:5] == b"Adobe" and len(seg) >= 12:
            transform = seg[11]
        elif marker == 0xDA:
            if not frame:
                raise ImageError("JPEG scan before its frame header")
            return _jpeg_scan(data, pos, seg, frame, qt, dc_t, ac_t, restart, transform)
    raise ImageError("JPEG has no image data")


def _jpeg_scan(data, pos, sos, frame, qt, dc_t, ac_t, restart, transform):
    w, h, comps = frame
    if w * h > MAX_PIXELS or not w or not h:
        raise ImageError("JPEG too large")
    if sos[0] != len(comps):
        raise ImageError("multi-scan JPEG isn't supported")
    if len(comps) not in (1, 3):
        raise ImageError("CMYK JPEG isn't supported")
    by_id = {c["id"]: c for c in comps}
    for k in range(sos[0]):
        c = by_id[sos[1 + k * 2]]
        c["dc"], c["ac"] = dc_t[sos[2 + k * 2] >> 4], ac_t[sos[2 + k * 2] & 15]
    # entropy-coded data, split at restart markers, with the 0xFF00 stuffing removed
    segs, cur, i = [], bytearray(), pos
    while i < len(data):
        j = data.find(b"\xff", i)
        if j < 0:
            cur += data[i:]
            break
        cur += data[i:j]
        nb = data[j + 1] if j + 1 < len(data) else 0xD9
        if nb == 0:
            cur.append(0xFF); i = j + 2
        elif 0xD0 <= nb <= 0xD7:
            segs.append(cur); cur = bytearray(); i = j + 2
        elif nb == 0xFF:
            i = j + 1
        else:
            break
    segs.append(cur)
    hmax, vmax = max(c["h"] for c in comps), max(c["v"] for c in comps)
    mcux, mcuy = -(-w // (8 * hmax)), -(-h // (8 * vmax))
    for c in comps:
        c["pw"] = mcux * c["h"] * 8
        c["plane"] = bytearray(c["pw"] * mcuy * c["v"] * 8)
        c["q"] = qt[c["tq"]]
    seg_i, bits, pred, left = 0, _Bits(segs[0]), [0] * len(comps), restart
    for my in range(mcuy):
        for mx in range(mcux):
            if restart:
                if not left:
                    seg_i += 1
                    bits, pred, left = _Bits(segs[seg_i] if seg_i < len(segs) else b""), [0] * len(comps), restart
                left -= 1
            for ci, c in enumerate(comps):
                q, plane, pw = c["q"], c["plane"], c["pw"]
                for v in range(c["v"]):
                    for hh in range(c["h"]):
                        blk = [0] * 64
                        t = bits.decode(c["dc"])
                        pred[ci] += _extend(bits.get(t), t)
                        blk[0] = pred[ci] * q[0]
                        k = 1
                        while k < 64:
                            rs = bits.decode(c["ac"])
                            r, s = rs >> 4, rs & 15
                            if not s:
                                if r != 15:
                                    break
                                k += 16
                                continue
                            k += r
                            if k > 63:
                                break
                            blk[_ZIGZAG[k]] = _extend(bits.get(s), s) * q[k]
                            k += 1
                        _idct_put(blk, plane, pw, (mx * c["h"] + hh) * 8, (my * c["v"] + v) * 8)
    out = bytearray(w * h * 4)
    planes = [(c["plane"], c["pw"], c["h"], c["v"]) for c in comps]
    o = 0
    for y in range(h):
        rows = [(p, pw * ((y * cv) // vmax), ch) for p, pw, ch, cv in planes]
        for x in range(w):
            vals = [p[row + (x * ch) // hmax] for p, row, ch in rows]
            if len(vals) == 1:
                r = g = b = vals[0]
            elif transform == 0:
                r, g, b = vals
            else:
                yy, cb, cr = vals[0], vals[1] - 128, vals[2] - 128
                r, g, b = _c(yy + 1.402 * cr), _c(yy - 0.344136 * cb - 0.714136 * cr), _c(yy + 1.772 * cb)
            out[o] = r; out[o + 1] = g; out[o + 2] = b; out[o + 3] = 255
            o += 4
    return Image(w, h, out)


def _idct_put(blk, plane, pw, x0, y0):
    tmp = [0.0] * 64
    T = _IDCT
    for v in range(8):
        row = blk[v * 8:v * 8 + 8]
        if not any(row[1:]):
            dc = row[0] * T[0][0]
            for x in range(8):
                tmp[v * 8 + x] = dc
            continue
        for x in range(8):
            t = T[x]
            tmp[v * 8 + x] = (t[0] * row[0] + t[1] * row[1] + t[2] * row[2] + t[3] * row[3] + t[4] * row[4]
                              + t[5] * row[5] + t[6] * row[6] + t[7] * row[7])
    for x in range(8):
        col = tmp[x::8]
        for y in range(8):
            t = T[y]
            val = (t[0] * col[0] + t[1] * col[1] + t[2] * col[2] + t[3] * col[3] + t[4] * col[4]
                   + t[5] * col[5] + t[6] * col[6] + t[7] * col[7]) + 128
            plane[(y0 + y) * pw + x0 + x] = _c(val)


# --------------------------------------------------------------------------- TGA and BLP (addon icon textures)

def decode_tga(data: bytes) -> Image:
    if len(data) < 18:
        raise ImageError("not an image")
    idlen, cmap, itype = data[0], data[1], data[2]
    w, h, bpp, desc = struct.unpack("<HHBB", data[12:18])
    if cmap or itype not in (2, 3, 10, 11) or bpp not in (8, 24, 32) or not w or not h or w * h > MAX_PIXELS:
        raise ImageError("unsupported or unknown image format")
    gray = itype in (3, 11)
    if gray != (bpp == 8):
        raise ImageError("unsupported TGA")
    n = bpp // 8
    pos = 18 + idlen
    if itype in (2, 3):
        raw = data[pos:pos + w * h * n]
    else:
        raw = bytearray()
        while len(raw) < w * h * n and pos < len(data):
            hdr = data[pos]; pos += 1
            count = (hdr & 0x7F) + 1
            if hdr & 0x80:
                raw += data[pos:pos + n] * count; pos += n
            else:
                raw += data[pos:pos + n * count]; pos += n * count
    if len(raw) < w * h * n:
        raise ImageError("TGA data is truncated")
    out = bytearray(w * h * 4)
    top_down = bool(desc & 0x20)
    for y in range(h):
        sy = y if top_down else h - 1 - y
        for x in range(w):
            i = (sy * w + x) * n
            o = (y * w + x) * 4
            if gray:
                out[o:o + 4] = bytes((raw[i], raw[i], raw[i], 255))
            else:
                out[o:o + 4] = bytes((raw[i + 2], raw[i + 1], raw[i], raw[i + 3] if n == 4 else 255))
    return Image(w, h, out)


def _rgb565(v):
    r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def decode_blp(data: bytes) -> Image:
    if data[:4] != b"BLP2" or len(data) < 1172:
        raise ImageError("not a BLP2 texture")
    comp, adepth, atype = data[8], data[9], data[10]
    w, h = struct.unpack("<II", data[12:20])
    if not w or not h or w * h > MAX_PIXELS:
        raise ImageError("BLP too large")
    off, size = struct.unpack("<I", data[20:24])[0], struct.unpack("<I", data[84:88])[0]
    mip = data[off:off + size]
    pal = data[148:1172]
    out = bytearray(w * h * 4)
    if comp == 1:  # 256-color palette + separate alpha
        if len(mip) < w * h:
            raise ImageError("BLP data is truncated")
        alpha = mip[w * h:]
        for i in range(w * h):
            p = mip[i] * 4
            if adepth == 8:
                a = alpha[i] if i < len(alpha) else 255
            elif adepth in (1, 4):
                per = 8 // adepth
                byte = alpha[i // per] if i // per < len(alpha) else 255
                a = ((byte >> ((i % per) * adepth)) & ((1 << adepth) - 1)) * (255 // ((1 << adepth) - 1))
            else:
                a = 255
            out[i * 4:i * 4 + 4] = bytes((pal[p + 2], pal[p + 1], pal[p], a))
    elif comp == 3:  # BGRA
        for i in range(min(w * h, len(mip) // 4)):
            b, g, r, a = mip[i * 4:i * 4 + 4]
            out[i * 4:i * 4 + 4] = bytes((r, g, b, a))
    elif comp == 2:  # DXT
        kind = 1 if adepth <= 1 else 5 if atype == 7 else 3
        bsize = 8 if kind == 1 else 16
        bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
        if len(mip) < bw * bh * bsize:
            raise ImageError("BLP data is truncated")
        for by in range(bh):
            for bx in range(bw):
                blk = mip[(by * bw + bx) * bsize:(by * bw + bx + 1) * bsize]
                alphas = None
                if kind == 3:
                    bits = int.from_bytes(blk[:8], "little")
                    alphas = [((bits >> (4 * k)) & 15) * 17 for k in range(16)]
                elif kind == 5:
                    a0, a1 = blk[0], blk[1]
                    tbl = [a0, a1] + ([((7 - k) * a0 + k * a1) // 7 for k in range(1, 7)] if a0 > a1 else
                                      [((5 - k) * a0 + k * a1) // 5 for k in range(1, 5)] + [0, 255])
                    bits = int.from_bytes(blk[2:8], "little")
                    alphas = [tbl[(bits >> (3 * k)) & 7] for k in range(16)]
                cb = blk[-8:]
                c0v, c1v = struct.unpack("<HH", cb[:4])
                c0, c1 = _rgb565(c0v), _rgb565(c1v)
                if kind != 1 or c0v > c1v:
                    cols = [c0 + (255,), c1 + (255,),
                            tuple((2 * a + b) // 3 for a, b in zip(c0, c1)) + (255,),
                            tuple((a + 2 * b) // 3 for a, b in zip(c0, c1)) + (255,)]
                else:
                    cols = [c0 + (255,), c1 + (255,), tuple((a + b) // 2 for a, b in zip(c0, c1)) + (255,),
                            (0, 0, 0, 0 if adepth else 255)]
                idx = struct.unpack("<I", cb[4:8])[0]
                for k in range(16):
                    x, y = bx * 4 + k % 4, by * 4 + k // 4
                    if x < w and y < h:
                        r, g, b, a = cols[(idx >> (2 * k)) & 3]
                        if alphas:
                            a = alphas[k]
                        out[(y * w + x) * 4:(y * w + x) * 4 + 4] = bytes((r, g, b, a))
    else:
        raise ImageError("unsupported BLP compression")
    return Image(w, h, out)


# =========================================================================== classic UI textures

# Gethe/wow-ui-textures is a mirror of the game's UI art. This commit is the "classic" branch at 1.13.5;
# pinning it keeps every download identical. WOWAM_ART_BASE points the tests at the mock server.
ART_COMMIT = "312a6e61bab69370c65ccc0a08f1075d62196294"
ICONS_COMMIT = "d23deaf8f44a7d280dc974a5c9d5321c013db59b"  # "live" branch, for Interface\Icons paths in .toc files
MIRROR = "https://raw.githubusercontent.com/Gethe/wow-ui-textures/"
ART_FILES = {
    "border": "DialogFrame/UI-DialogBox-Border.PNG",
    "header": "DialogFrame/UI-DialogBox-Header.PNG",
    "button_up": "Buttons/UI-Panel-Button-Up.PNG",
    "button_down": "Buttons/UI-Panel-Button-Down.PNG",
    "button_hl": "Buttons/UI-Panel-Button-Highlight.PNG",
    "button_off": "Buttons/UI-Panel-Button-Disabled.PNG",
    "check_up": "Buttons/UI-CheckBox-Up.PNG",
    "check_down": "Buttons/UI-CheckBox-Down.PNG",
    "check_mark": "Buttons/UI-CheckBox-Check.PNG",
    "radio": "Buttons/UI-RADIOBUTTON.PNG",
    "knob": "Buttons/UI-ScrollBar-Knob.PNG",
    "arrow_up": "Buttons/UI-ScrollBar-ScrollUpButton-Up.PNG",
    "arrow_down": "Buttons/UI-ScrollBar-ScrollDownButton-Up.PNG",
    "input": "COMMON/Common-Input-Border.PNG",
    "question": "ICONS/INV_Misc_QuestionMark.PNG",
    "slot": "Buttons/UI-Quickslot2.PNG",  # the action-button frame drawn around icons
    "rock": "FrameGeneral/UI-Background-Rock.PNG",  # the stone behind the game's panels
    "parchment": "AdventureMap/AdventureMapParchmentTile.PNG",  # a seamlessly tiling quest parchment
}


def art_base() -> str:
    return os.environ.get("WOWAM_ART_BASE") or MIRROR + ART_COMMIT + "/"


def art_dir() -> Path:
    return wowam.config_dir() / "art"


def _fetch(url: str) -> bytes:
    if not url.lower().startswith(("https://", "http://")):
        raise wowam.WowamError(f"Not a web address: {url}")
    return wowam._http_get(url, MAX_DOWNLOAD)


def _save(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def ensure_art(log=None) -> bool:
    """Download any missing texture (once; they're cached). True when the full set is on disk."""
    for name, rel in ART_FILES.items():
        dest = art_dir() / f"{name}.png"
        if dest.is_file():
            continue
        try:
            data = _fetch(art_base() + rel)
            decode_png(data)  # only keep what we can actually read
            _save(dest, data)
        except (wowam.WowamError, ImageError, OSError) as e:
            if log:
                log(f"! Couldn't get the Warcraft UI art ({rel}): {e}")
            return False  # offline or blocked: don't try the rest one by one
    return True


def load_art() -> dict | None:
    try:
        return {n: decode_png((art_dir() / f"{n}.png").read_bytes()) for n in ART_FILES}
    except (OSError, ImageError):
        return None


def brightness(img: Image, k: float) -> Image:
    """A copy with every color channel scaled by k (alpha untouched)."""
    out = img.copy()
    px = out.px
    for i in range(0, len(px), 4):
        px[i], px[i + 1], px[i + 2] = _c(px[i] * k), _c(px[i + 1] * k), _c(px[i + 2] * k)
    return out


def backdrop(art: dict, edge: int, inner: Image, center: int = 240, inset: float = 0.3) -> Image:
    """A dialog-box frame (WoW's backdrop: 8 pieces in one strip, top/bottom stored rotated) around a tiled
    texture, as a 9-slice image: edge-sized corners, and a middle row/column that ttk can tile seamlessly
    (center is a multiple of edge). Outside the frame it is transparent."""
    strip = art["border"]
    seg = strip.w // 8
    pieces = [strip.crop(i * seg, 0, seg, strip.h).scale(edge, edge) for i in range(8)]
    left, right, top, bottom = pieces[0], pieces[1], pieces[2].rotate(True), pieces[3].rotate(True)
    tl, tr, bl, br = pieces[4:8]
    n = 2 * edge + center
    img = Image(n, n)
    ins = int(edge * inset)
    tex = Image(n - 2 * ins, n - 2 * ins)  # inner must tile seamlessly with period == center
    inner.tile_into(tex, 0, 0, tex.w, tex.h)
    img.over(tex, ins, ins)
    for k in range(edge, n - edge, edge):
        img.over(left, 0, k).over(right, n - edge, k)
        img.over(top, k, 0).over(bottom, k, n - edge)
    img.over(tl, 0, 0).over(tr, n - edge, 0).over(bl, 0, n - edge).over(br, n - edge, n - edge)
    return img


def three_slice(img: Image, width: int, cap: int) -> Image:
    """Stretch an image horizontally by tiling its middle, keeping cap pixels at each end."""
    width = max(width, 2 * cap + 1)
    out = Image(width, img.h)
    out.over(img.crop(0, 0, cap, img.h), 0, 0)
    out.over(img.crop(img.w - cap, 0, cap, img.h), width - cap, 0)
    img.crop(cap, 0, img.w - 2 * cap, img.h).tile_into(out, cap, 0, width - 2 * cap, img.h)
    return out


def three_slice_v(img: Image, height: int, cap: int) -> Image:
    """Like three_slice, vertically."""
    return three_slice(img.rotate(True), height, cap).rotate(False)


def bezel_mask(img: Image, solid: int = 240):
    """For a button texture: a function that keeps only the solid button (bezel and face) and crops to it.
    Around it the texture has a semi-transparent rim in the button's color, meant to soften it against game
    art; on a flat window it reads as the red spilling past the bezel."""
    keep = bytearray(1 if img.px[i + 3] >= solid else 0 for i in range(0, len(img.px), 4))
    xs = [i % img.w for i, k in enumerate(keep) if k]
    ys = [i // img.w for i, k in enumerate(keep) if k]
    if not xs:
        return lambda im: im
    box = (min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)

    def apply(im: Image) -> Image:
        out = im.copy()
        for i, k in enumerate(keep):
            if not k:
                out.px[i * 4 + 3] = 0
        return out.crop(*box)
    return apply


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(rgb[:3])


def build_kit(art: dict, button_h: int = 22, edge: int = 24) -> dict:
    """Every image the Warcraft theme needs. They keep their transparency: Tk blends a photo's alpha over
    whatever the widget drew underneath, which here is a tiled texture (like every panel in the game)."""
    k = {}
    # textures: dark stone for the window, darker for lists, quest parchment for the log
    k["bg"] = brightness(art["rock"], 0.62)
    k["list"] = brightness(art["rock"], 0.34)
    k["parchment"] = brightness(art["parchment"], 1.3)
    k["bg_color"], k["list_color"] = _hex(k["bg"].average()), _hex(k["list"].average())
    k["parchment_color"] = _hex(k["parchment"].average())
    k["panel"] = backdrop(art, edge, k["list"].scale(10 * edge, 10 * edge), center=10 * edge)  # whole tiles
    k["sel"] = Image(8, 8, fill=(255, 200, 0, 72))  # the gold glow of a selected quest-log line
    k["clear"] = Image(8, 8)

    outline = bezel_mask(art["button_up"].crop_frac(0, 0.625, 0, 0.6875))

    def btn(name, hl=False):
        b = art[name].crop_frac(0, 0.625, 0, 0.6875)  # where WoW's UIPanelButtonTemplate samples it
        if hl:
            b.add(art["button_hl"].crop_frac(0, 0.625, 0, 0.6875))
        b = outline(b)  # only what's inside the metal bezel: the soft red rim outside it is meant for game art
        # narrow (caps + a little middle): ttk makes an image's width the button's minimum, and tiles the
        # middle; heightened by repeating the plain middle rows, so the bezel stays one pixel crisp
        return three_slice_v(three_slice(b, 28, 10), button_h, 7)
    k["button"], k["button_active"] = btn("button_up"), btn("button_up", hl=True)
    k["button_pressed"], k["button_disabled"] = btn("button_down"), btn("button_off")

    def box(name, mark=False, alpha=1.0):
        im = art[name].crop(3, 3, 24, 24)
        if mark:
            im.over(art["check_mark"].crop(3, 3, 24, 24))
        return Image(im.w, im.h).over(im, opacity=alpha).scale(18, 18)
    k["check_off"], k["check_on"] = box("check_up"), box("check_up", True)
    k["check_pressed"], k["check_disabled"] = box("check_down"), box("check_up", alpha=0.45)
    seg = art["radio"].w // 4
    ring, dot = art["radio"].crop(0, 0, seg, art["radio"].h), art["radio"].crop(seg, 0, seg, art["radio"].h)
    k["radio_off"], k["radio_on"] = ring, ring.copy().over(dot)

    def tight(im, w, h):  # crop to the drawn pixels (these textures sit in a corner of a 32x32 canvas)
        box = im.bbox() or (0, 0, im.w, im.h)
        return im.crop(*box).scale(w, h)
    k["thumb"] = tight(art["knob"], 16, 15)
    for name in ("arrow_up", "arrow_down"):
        k[name] = tight(art[name], 16, 15)

    k["entry"] = art["input"].crop(0, 0, art["input"].w, 20)
    mid = k["entry"].crop(k["entry"].w // 2, 10, 1, 1)
    k["entry_field"] = _hex(Image(1, 1, fill=tuple(k["bg"].average()) + (255,)).over(mid).get(0, 0))
    k["header"] = art["header"].crop(58, 0, 140, 40)
    k["question"] = art["question"]
    slot = art["slot"]
    k["slot"] = slot.crop(*(slot.bbox() or (0, 0, slot.w, slot.h)))
    return k


def plaque(kit: dict, width: int) -> Image:
    """The dialog title plaque, widened to fit a title."""
    return three_slice(kit["header"], width, 22)


# =========================================================================== addon icons

class IconStore:
    """Addon icons as 64x64 PNGs in <config>/icons, keyed like the state file (mod id or gh:owner/repo).
    Lookups that found nothing are remembered for a few days, so we don't re-download on every start."""
    SIZE = 64
    RETRY = 3 * 86400

    def __init__(self):
        import threading
        self.dir = wowam.config_dir() / "icons"
        self.lock = threading.Lock()
        try:
            self.misses = json.loads((self.dir / "misses.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.misses = {}

    def _file(self, key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)
        if safe != key:
            safe += "-" + hashlib.sha1(key.encode()).hexdigest()[:8]
        return self.dir / f"{safe}.png"

    def cached(self, key: str) -> Image | None:
        try:
            return decode_png(self._file(key).read_bytes())
        except (OSError, ImageError):
            return None

    def recently_missed(self, key: str) -> bool:
        return time.time() - self.misses.get(key, 0) < self.RETRY

    def _remember(self, key: str, img: Image | None) -> Image | None:
        with self.lock:
            if img is None:
                self.misses[key] = int(time.time())
            else:
                self.misses.pop(key, None)
                img = _square(img, self.SIZE)
                _save(self._file(key), img.to_png())
            try:
                _save(self.dir / "misses.json", json.dumps(self.misses).encode())
            except OSError:
                pass
        return img

    def fetch(self, key: str, addons_dir=None, folders=(), logo_url: str | None = None,
              remember_miss: bool = True) -> Image | None:
        """Find an icon (worker thread). The .toc IconTexture comes first: it's what the game's addon list shows.
        A network failure isn't remembered as "no icon", so it's retried on the next start."""
        img = None
        if addons_dir and folders:
            img = toc_icon(Path(addons_dir), folders)
        if img is None and logo_url:
            try:
                data = _fetch(logo_url)
            except wowam.WowamError:
                return None
            try:
                img = decode(data)
            except (ImageError, ValueError, IndexError, KeyError, struct.error, zlib.error):
                img = None
        if img is None and not remember_miss:
            return None
        return self._remember(key, img)


def _square(img: Image, size: int) -> Image:
    """Center-crop to a square, then scale to size x size."""
    s = min(img.w, img.h)
    if img.w != img.h:
        img = img.crop((img.w - s) // 2, (img.h - s) // 2, s, s)
    return img.scale(size, size)


def _ring(img: Image, d: int):
    """Mean and spread of the luminance of the pixels exactly d pixels in from the edge."""
    lum = []
    for y in range(img.h):
        for x in range(img.w):
            if min(x, y, img.w - 1 - x, img.h - 1 - y) == d:
                i = (y * img.w + x) * 4
                lum.append(0.3 * img.px[i] + 0.59 * img.px[i + 1] + 0.11 * img.px[i + 2])
    mean = sum(lum) / len(lum)
    return mean, (sum((v - mean) ** 2 for v in lum) / len(lum)) ** 0.5


def has_icon_border(img: Image) -> bool:
    """Does this 64x64 icon already carry the game's baked-in icon border? Blizzard's icons (measured on
    the classic and retail files) have, from the edge in: an optional black pixel, two light bevel rings
    (mean luminance ~85-115), then a dark line (~10)."""
    if (img.w, img.h) != (64, 64):
        img = _square(img, 64)
    m = [_ring(img, d)[0] for d in range(4)]
    return ((m[0] <= 15 and m[1] >= 70 and m[2] >= 70 and m[3] <= 25)
            or (m[0] >= 70 and m[1] >= 70 and m[2] <= 25))


def framed(img: Image, size: int, frame: Image | None = None) -> Image:
    """A list icon. With the game's action-button frame (UI-Quickslot2), an icon that lacks the WoW icon
    border is set inside that frame; one that already has it (Blizzard's own icons) is shown as it is.
    Without the art, a plain 1px dark frame."""
    if frame is None:
        out = Image(size, size, fill=(0, 0, 0, 255))
        return out.over(img.scale(size - 2, size - 2), 1, 1)
    if has_icon_border(img):
        return Image(size, size, fill=(0, 0, 0, 255)).over(img.scale(size, size))
    inset = max(2, round(size * 0.065))
    out = Image(size, size)
    out.over(Image(size - 2 * inset + 2, size - 2 * inset + 2, fill=(0, 0, 0, 255)), inset - 1, inset - 1)
    out.over(img.scale(size - 2 * inset, size - 2 * inset), inset, inset)
    return out.over(frame.scale(size, size))


# =========================================================================== the game's font

def ttf_family(path) -> str | None:
    """The family name stored in a TrueType/OpenType file's name table."""
    try:
        d = Path(path).read_bytes()
        n = struct.unpack(">H", d[4:6])[0]
        for i in range(n):
            tag, _, off, _ = struct.unpack(">4sIII", d[12 + 16 * i:28 + 16 * i])
            if tag != b"name":
                continue
            _, count, strings = struct.unpack(">HHH", d[off:off + 6])
            found = {}
            for k in range(count):
                plat, enc, _, nid, ln, so = struct.unpack(">HHHHHH", d[off + 6 + 12 * k:off + 18 + 12 * k])
                if nid == 1:
                    raw = d[off + strings + so:off + strings + so + ln]
                    found[plat] = raw.decode("utf-16-be" if plat in (0, 3) else "latin-1", "replace")
            return found.get(3) or found.get(1) or found.get(0)
    except (OSError, struct.error, IndexError):
        pass
    return None


def fonts_dir() -> Path:
    return wowam.config_dir() / "fonts"


def find_wow_font(addons_dir=None) -> Path | None:
    """The game's UI font, FRIZQT__.TTF (Friz Quadrata), wherever this computer already has a copy: one picked
    in Settings (kept in <config>/fonts), any WoW install folder's Fonts, or an addon that bundles it.
    It is a commercial typeface, so wowam never downloads it."""
    cands = sorted(fonts_dir().glob("*.[tToO][tT][fF]")) if fonts_dir().is_dir() else []
    if addons_dir:
        a = Path(addons_dir)
        wow = a.parent.parent.parent  # World of Warcraft/<flavor>/Interface/AddOns
        dirs = [p / "Fonts" for p in (wow.glob("_*_") if wow.is_dir() else [])] + [wow / "Fonts"]
        for d in dirs:
            if d.is_dir():
                cands += [p for p in d.iterdir() if p.name.lower() == "frizqt__.ttf"]
        if a.is_dir():
            for depth in ("*/*", "*/*/*", "*/*/*/*"):
                cands += [p for p in a.glob(depth) if p.name.lower() == "frizqt__.ttf"]
    return next((p for p in cands if p.is_file()), None)


def register_font(path) -> str | None:
    """Make a font file usable by Tk for this process only (nothing is installed). Returns its family name."""
    family = ttf_family(path)
    if not family:
        return None
    try:
        import ctypes
        if os.name == "nt":
            ok = ctypes.windll.gdi32.AddFontResourceExW(str(path), 0x10, 0)  # FR_PRIVATE
        else:  # Linux/BSD Tk draws with Xft, which asks fontconfig
            import ctypes.util
            lib = ctypes.util.find_library("fontconfig")
            ok = lib and ctypes.CDLL(lib).FcConfigAppFontAddFile(None, str(path).encode())
        return family if ok else None
    except (OSError, AttributeError):
        return None


def _find_ci(path: Path) -> Path | None:
    """Like path.is_file(), but case-insensitive (WoW paths are; Linux file systems aren't)."""
    if path.is_file():
        return path
    try:
        for p in path.parent.iterdir():
            if p.name.lower() == path.name.lower() and p.is_file():
                return p
    except OSError:
        pass
    return None


def toc_icon(addons_dir: Path, folders) -> Image | None:
    """Decode the texture a folder's .toc names in `## IconTexture:` (BLP, TGA, PNG or JPEG)."""
    root = addons_dir.resolve()
    for folder in folders:
        tex = wowam.read_toc(addons_dir / folder).get("IconTexture", "").strip().replace("\\", "/")
        if not tex:
            continue
        low = tex.lower()
        if low.startswith("interface/") and not low.startswith("interface/addons/"):
            # the game's own art (Interface\Icons\..., Interface\HELPFRAME\...): from the mirror, whose folder
            # names are mostly upper case; GitHub paths are case-sensitive, so try the likely spellings
            base = art_base() if os.environ.get("WOWAM_ART_BASE") else MIRROR + ICONS_COMMIT + "/"
            rest = re.sub(r"\.(blp|tga|png)$", "", tex[len("interface/"):], flags=re.I).split("/")
            tries = dict.fromkeys(["/".join(rest), "/".join([rest[0].upper()] + rest[1:])])
            for t in tries:
                try:
                    return decode_png(_fetch(f"{base}{t}.PNG"))
                except (wowam.WowamError, ImageError):
                    continue
            continue
        base = addons_dir / tex[len("interface/addons/"):] if low.startswith("interface/addons/") \
            else addons_dir / folder / tex
        try:
            if root not in base.resolve().parents:
                continue  # a .toc pointing outside AddOns
        except OSError:
            continue
        for ext in ("", ".blp", ".tga", ".png", ".jpg", ".jpeg"):
            p = _find_ci(base.with_name(base.name + ext))
            if p and p.stat().st_size <= MAX_DOWNLOAD:
                try:
                    return decode(p.read_bytes())
                except (ImageError, ValueError, IndexError, struct.error, zlib.error):
                    break
    return None


def logo_url(mod: dict) -> str | None:
    logo = mod.get("logo") or {}
    return logo.get("thumbnailUrl") or logo.get("url") or None


# =========================================================================== the frog

_FROG_BODY = [
    "...ooo....ooo...",
    "..oWWPo..oPWWo..",
    "..oWWPo..oPWWo..",
    ".ooGGGGooGGGGoo.",
    ".oGGGGGGGGGGGGo.",
    "oGGGGGGGGGGGGGGo",
    "oGGkGGGGGGGGkGGo",
    "oGGGkkkkkkkkGGGo",
]
FROG_SIT = ["................", "................"] + _FROG_BODY + [
    ".oGLLLLLLLLLLGo.",
    "oGGoLLLLLLLLoGGo",
    "oGGGoooooooooGGo",
    ".ooo.......oooo.",
]
FROG_LEAP = _FROG_BODY + [
    ".oGLLLLLLLLLLGo.",
    "..oGoLLLLLLoGo..",
    "..oGo.oooo.oGo..",
    ".oGGo......oGGo.",
    ".oGGo......oGGo.",
    "oGGo........oGGo",
]
_FROG_COLORS = {"o": (24, 46, 14), "G": (86, 168, 58), "L": (178, 222, 120), "W": (250, 250, 240),
                "P": (10, 10, 10), "k": (40, 78, 24)}


def frog(leap: bool = False, scale: int = 2) -> Image:
    """Our own pixel-art frog (sitting, or mid-leap with its legs out)."""
    rows = FROG_LEAP if leap else FROG_SIT
    img = Image(len(rows[0]) * scale, len(rows) * scale)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            c = _FROG_COLORS.get(ch)
            if c:
                for dy in range(scale):
                    for dx in range(scale):
                        img.put(x * scale + dx, y * scale + dy, c + (255,))
    return img
