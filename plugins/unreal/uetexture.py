# Copyright (C) 2026 xsm909
#
# This file is part of xverb-plugins.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""A texture's source picture, out of an Unreal package, as a PNG.

**The source is what was imported, not what the game draws.** An editor
package keeps it in the texture's `Source`: its size, slices, mips and pixel
format are tagged properties; the pixels are a compressed buffer (see
`uemesh.buffers`) holding one of

- a PNG or a JPEG file as it was imported — handed on as it is;
- raw pixels, Oodle-compressed;
- raw pixels after the **UE Delta** transform, then Oodle-compressed.

**UE Delta**, worked out from the files, since its code is not in an
install: the picture is cut into bands of 32 768 pixels (so many rows as that
is, at least one); the first row of a band is stored as it is and every other
row as its byte-wise difference from the row above, modulo 256. The band
height is also measured from the data — the first rows of bands are the only
ones that look like pictures — and the measurement wins where it disagrees,
as it does for a 2048-wide virtual texture. 8-bit sources come out red first
though the format is called BGRA8, and half-float ones blue first: both
measured against the editor's thumbnails, which match imported PNGs.

Everything is turned into 8-bit RGBA and written as a PNG, at most 4096 on a
side: floating-point sources are clamped to 0..1 and shown linear, as the
editor's thumbnails show them.
"""

from __future__ import annotations

import math
import struct
import zlib
from typing import List, Optional, Tuple

import ueobject
import uemesh
from uasset import Package, UassetError, _Reader

#: Bytes a pixel, by source format.
_BYTES = {"TSF_G8": 1, "TSF_BGRA8": 4, "TSF_BGRE8": 4, "TSF_RGBA16": 8, "TSF_RGBA16F": 8,
          "TSF_G16": 2, "TSF_RGBA32F": 16, "TSF_R16F": 2, "TSF_R32F": 4,
          "TSF_RGBA8_DEPRECATED": 4, "TSF_RGBE8_DEPRECATED": 4}

LARGEST = 4096


class TextureError(Exception):
    pass


class Source:
    def __init__(self, width: int, height: int, slices: int, mips: int, fmt: str,
                 compression: str, png: bool):
        self.width = width
        self.height = height
        self.slices = slices
        self.mips = mips
        self.format = fmt
        self.compression = compression
        self.png = png


# -- the older way: a bulk data block after the texture's properties -----------------

_PAYLOAD_AT_END = 0x01
_ZLIB = 0x02
_UNUSED = 0x20
_SEPARATE_FILE = 0x100
_SIZE_64 = 0x2000


def _zlib_chunks(data: bytes, at: int) -> bytes:
    """`SerializeCompressed`: the package tag, a summary, chunk sizes, zlib streams."""
    tag, chunk = struct.unpack_from("<qq", data, at)
    if tag & 0xFFFFFFFF != 0x9E2A83C1:
        raise TextureError("The texture's compressed source is damaged.")
    packed, whole = struct.unpack_from("<qq", data, at + 16)
    count = (whole + chunk - 1) // chunk if chunk else 0
    sizes = [struct.unpack_from("<qq", data, at + 32 + 16 * i) for i in range(count)]
    pos = at + 32 + 16 * count
    out = bytearray()
    for compressed, _ in sizes:
        out += zlib.decompress(data[pos:pos + compressed])
        pos += compressed
    return bytes(out)


def _legacy_payload(p: Package, after: int) -> bytes:
    """The source's bulk data, found after the texture's own properties: the
    object's guid flag, two bytes of strip flags, then the bulk data header."""
    data = p.data
    r = _Reader(data, after)
    if r.i32():
        r.skip(16)
    r.skip(2)
    flags = r.u32()
    if flags & _SIZE_64:
        count, size = r.i64(), r.i64()
    else:
        count, size = r.i32(), r.i32()
    offset = r.i64()
    if flags & _UNUSED or count <= 0:
        raise TextureError("No source pixels were found in this package.")
    if flags & _SEPARATE_FILE:
        raise TextureError("This texture keeps its source in a separate .ubulk file.")
    at = p.bulk_start + offset if flags & _PAYLOAD_AT_END else r.at
    if flags & _ZLIB:
        return _zlib_chunks(data, at)
    return data[at:at + count]


def source(p: Package) -> Optional[Source]:
    for export in ueobject.exports(p):
        if export.klass not in ("Texture2D", "TextureCube", "Texture2DArray", "VolumeTexture",
                                "TextureLightProfile"):
            continue
        tags, after = ueobject.properties(p, export)
        found = next((t for t in tags if t.name == "Source"), None)
        if found is None:
            return None
        inner = {t.name: t for t in ueobject.tags(p, _Reader(p.data, found.at), found.at + found.size)}

        def number(name: str, default: int = 0) -> int:
            return ueobject.int_value(p, inner[name]) if name in inner else default

        def word(name: str) -> str:
            tag = inner.get(name)
            if tag is None:
                return ""
            if tag.size == 8:
                return ueobject.name_value(p, tag)
            return ""

        made = Source(number("SizeX"), number("SizeY"), number("NumSlices", 1),
                      number("NumMips", 1), word("Format") or "TSF_BGRA8",
                      word("CompressionFormat") or "TSCF_None",
                      bool(inner["bPNGCompressed"].bool) if "bPNGCompressed" in inner else False)
        made.after = after
        return made
    return None


def _payload(data: bytes) -> bytes:
    for buffer in uemesh.buffers(data):
        return uemesh.decompress(data, *buffer)
    raise TextureError("No source pixels were found in this package.")


# -- UE Delta -------------------------------------------------------------------------

def _band_rows(raw: bytes, width: int, height: int, bpp: int) -> int:
    """How many rows a band has: measured, with 32 768 pixels as the guess."""
    guess = max(1, 32768 // max(1, width))
    row = width * bpp
    if height < 4 or row == 0:
        return guess
    step = max(1, row // 256)

    def looks_raw(y: int) -> float:
        piece = raw[y * row:(y + 1) * row:step]
        return sum(min(b, 256 - b) for b in piece) / max(1, len(piece))

    candidates = sorted({guess, guess * 2, guess * 4, 16, 32, 64, 128, 256})
    scored = []
    for n in candidates:
        if n >= height:
            continue
        firsts = list(range(n, height, n))[:24]
        others = [y + n // 2 for y in firsts if 0 < n // 2 and y + n // 2 < height][:24]
        if not firsts or not others:
            continue
        scored.append((sum(map(looks_raw, firsts)) / len(firsts)
                       - sum(map(looks_raw, others)) / len(others), n))
    if not scored:
        return guess
    best = max(s for s, _ in scored)
    if best < 8:
        # Nothing stands out — a dark or flat picture, where a wrong guess
        # does not show either.
        return guess
    return min(n for s, n in scored if s >= best * 0.8)


def undelta(raw: bytes, width: int, height: int, bpp: int) -> bytes:
    """UE Delta undone: each row plus the one above, byte by byte, in bands.

    Rows are added as whole integers with the carries masked out between
    bytes — one Python operation a row rather than one a byte.
    """
    row = width * bpp
    if row == 0:
        return raw
    band = _band_rows(raw, width, height, bpp)
    low = int.from_bytes(b"\x7f" * row, "little")
    high = int.from_bytes(b"\x80" * row, "little")
    out = bytearray(raw[:row * height])
    above = 0
    for y in range(height):
        at = y * row
        current = int.from_bytes(out[at:at + row], "little")
        if y % band:
            current = (((current & low) + (above & low)) ^ ((current ^ above) & high))
            out[at:at + row] = current.to_bytes(row, "little")
        above = current
    return bytes(out)


# -- pixels to RGBA ------------------------------------------------------------------

def _srgb(v: float) -> int:
    if not v > 0.0:
        return 0
    if v >= 1.0:
        return 255
    s = v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055
    return int(s * 255 + 0.5)


def _rgba(raw: bytes, width: int, height: int, fmt: str, swapped: bool) -> bytes:
    """8-bit RGBA, top row first. ``swapped`` is BGRA bytes to be turned round."""
    n = width * height
    out = bytearray(n * 4)
    if fmt == "TSF_G8":
        g = raw[:n]
        out[0::4] = g
        out[1::4] = g
        out[2::4] = g
        out[3::4] = b"\xff" * n
    elif fmt in ("TSF_BGRA8", "TSF_RGBA8_DEPRECATED"):
        src = raw[:n * 4]
        if swapped:
            out[0::4] = src[2::4]
            out[1::4] = src[1::4]
            out[2::4] = src[0::4]
            out[3::4] = src[3::4]
        else:
            out[:] = src
    elif fmt in ("TSF_RGBA16", "TSF_G16"):
        step = 8 if fmt == "TSF_RGBA16" else 2
        src = raw[:n * step]
        if fmt == "TSF_G16":
            g = src[1::2]
            out[0::4] = g
            out[1::4] = g
            out[2::4] = g
        else:
            # The high byte of each little-endian 16-bit channel.
            for c in range(4):
                out[c::4] = src[2 * c + 1::8]
            return bytes(out)
        out[3::4] = b"\xff" * n
    elif fmt in ("TSF_BGRE8", "TSF_RGBE8_DEPRECATED"):
        src = raw[:n * 4]
        for i in range(n):
            b, g, r, e = src[i * 4:i * 4 + 4]
            f = math.ldexp(1.0, e - 136) if e else 0.0
            out[i * 4:i * 4 + 4] = bytes((_srgb(r * f), _srgb(g * f), _srgb(b * f), 255))
    elif fmt in ("TSF_RGBA16F", "TSF_R16F", "TSF_RGBA32F", "TSF_R32F"):
        # Linear and clamped, the way the editor's own thumbnail shows a
        # floating-point source: these are mostly data, not photographs.
        code = "e" if fmt.endswith("16F") else "f"
        channels = 4 if fmt.startswith("TSF_RGBA") else 1
        values = struct.unpack("<%d%s" % (n * channels, code),
                               raw[:n * channels * struct.calcsize(code)])
        table = {}
        for i in range(n):
            if channels == 4:
                # Blue first, whatever the name says — measured against the
                # editor's thumbnails.
                b, g, r, a = values[i * 4:i * 4 + 4]
            else:
                r = g = b = values[i]
                a = 1.0
            pixel = []
            for v in (r, g, b):
                key = round(v, 4)
                c = table.get(key)
                if c is None:
                    c = table[key] = max(0, min(255, int(v * 255 + 0.5))) if v == v else 0
                pixel.append(c)
            pixel.append(max(0, min(255, int(a * 255 + 0.5))) if a == a else 255)
            out[i * 4:i * 4 + 4] = bytes(pixel)
    else:
        raise TextureError("A pixel format this cannot show (%s)." % fmt)
    return bytes(out)


def _shrink(raw: bytes, width: int, height: int, bpp: int,
            largest: int = LARGEST) -> Tuple[bytes, int, int]:
    """Every n-th pixel of every n-th row, until it fits in ``largest``."""
    k = max(1, math.ceil(max(width, height) / largest))
    if k == 1:
        return raw, width, height
    row = width * bpp
    w2, h2 = (width + k - 1) // k, (height + k - 1) // k
    out = bytearray()
    for y in range(0, height, k):
        line = raw[y * row:(y + 1) * row]
        picked = bytearray(w2 * bpp)
        for c in range(bpp):
            picked[c::bpp] = line[c::bpp * k][:w2]
        out += picked
    return bytes(out), w2, h2


def png(rgba: bytes, width: int, height: int) -> bytes:
    row = width * 4
    raw = b"".join(b"\0" + rgba[y * row:(y + 1) * row] for y in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(
            ">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 3)) + chunk(b"IEND", b""))


def picture(p: Package, largest: int = LARGEST) -> Tuple[bytes, str, Source]:
    """The source as a picture file: its bytes, its media type, and what it was."""
    src = source(p)
    if src is None:
        raise TextureError("This texture keeps no source picture.")
    if any(True for _ in uemesh.buffers(p.data)):
        payload = _payload(p.data)
    else:
        payload = _legacy_payload(p, src.after)
    if payload[:8] == b"\x89PNG\r\n\x1a\n":
        return payload, "image/png", src
    if payload[:3] == b"\xff\xd8\xff":
        return payload, "image/jpeg", src
    if src.compression in ("TSCF_JPEG", "TSCF_UEJPEG", "TSCF_PNG") or src.png:
        raise TextureError("This texture's source is compressed in a way this cannot read.")
    bpp = _BYTES.get(src.format)
    if bpp is None:
        raise TextureError("A pixel format this cannot show (%s)." % src.format)
    width, height = src.width, src.height
    need = width * height * bpp
    if width <= 0 or height <= 0 or len(payload) < need:
        raise TextureError("The texture's pixels are fewer than its size says.")
    # The first slice's top mip comes first, whatever follows it.
    raw = payload[:need]
    delta = src.compression == "TSCF_UEDELTA"
    if delta:
        raw = undelta(raw, width, height, bpp)
    raw, width, height = _shrink(raw, width, height, bpp, largest)
    # 8-bit sources come out red first, delta-coded or not, though the format
    # is called BGRA8 — measured against the editor's thumbnails.
    rgba = _rgba(raw, width, height, src.format, swapped=False)
    alpha = rgba[3::4]
    if not alpha.strip(b"\0"):
        # An alpha channel of nothing but zeroes is an unused one, not a
        # picture that cannot be seen: shown opaque.
        rgba = bytearray(rgba)
        rgba[3::4] = b"\xff" * (width * height)
        rgba = bytes(rgba)
    return png(rgba, width, height), "image/png", src
