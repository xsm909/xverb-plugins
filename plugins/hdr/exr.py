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

"""OpenEXR, read with the standard library.

**What is read.** Scan-line and tiled images, one part or several, with any
number of channels in half, float or uint — and the compressions a render or a
texture is actually saved with: none, RLE, ZIPS, ZIP, PIZ, PXR24, B44 and B44A.
DWAA and DWAB are not: they are a JPEG-like transform with a Huffman stage and
a zip stage of their own. (macOS's ImageIO does not read them either —
measured on the fixtures, 2026-09-25.)

**What comes back is channels, not a picture.** Each channel is its samples in
the file's own bytes, row after row — half as two bytes, float and uint as
four. Which channels make the picture, and how light becomes a colour on a
screen, is `tone.py`'s business: this module knows the format and nothing
about looking at it.

**Speed is the whole difficulty.** A 1080p render is six million samples, and
anything done to each of them in Python is seconds. So every step is written
to be a slice, a `translate`, an `accumulate` or a `zlib` call — loops in C —
and the per-sample Python that is left is where the format leaves no choice:
PIZ's Huffman codes and its wavelet, and B44's blocks.

The layout references are the OpenEXR file layout document and, for the
compressors, the reference implementation's `ImfPizCompressor`,
`ImfHuf`, `ImfWav`, `ImfPxr24Compressor` and `ImfB44Compressor`.
"""

from __future__ import annotations

import struct
import sys
import zlib
from array import array
from itertools import accumulate
from typing import Dict, List, Optional, Tuple

MAGIC = b"\x76\x2f\x31\x01"

UINT, HALF, FLOAT = 0, 1, 2
SAMPLE_BYTES = {UINT: 4, HALF: 2, FLOAT: 4}
TYPE_NAMES = {UINT: "uint", HALF: "half", FLOAT: "float"}

NONE, RLE, ZIPS, ZIP, PIZ, PXR24, B44, B44A, DWAA, DWAB = range(10)
COMPRESSION_NAMES = {
    NONE: "none", RLE: "RLE", ZIPS: "ZIPS", ZIP: "ZIP", PIZ: "PIZ",
    PXR24: "PXR24", B44: "B44", B44A: "B44A", DWAA: "DWAA", DWAB: "DWAB",
}
#: Scan lines in one chunk, per compression.
LINES = {NONE: 1, RLE: 1, ZIPS: 1, ZIP: 16, PIZ: 32, PXR24: 16,
         B44: 32, B44A: 32, DWAA: 32, DWAB: 256}
READABLE = {NONE, RLE, ZIPS, ZIP, PIZ, PXR24, B44, B44A}

LINE_ORDERS = {0: "increasing y", 1: "decreasing y", 2: "random"}
LEVEL_MODES = {0: "one level", 1: "mipmap", 2: "ripmap"}

LITTLE = sys.byteorder == "little"


class ExrError(Exception):
    pass


# -- the header ------------------------------------------------------------


class Channel:
    __slots__ = ("name", "type", "linear", "xs", "ys")

    def __init__(self, name: str, kind: int, linear: bool, xs: int, ys: int):
        self.name, self.type, self.linear, self.xs, self.ys = name, kind, linear, xs, ys

    @property
    def size(self) -> int:
        return SAMPLE_BYTES.get(self.type, 4)


class Part:
    """One header: a whole file, or one part of a multi-part one."""

    def __init__(self, attributes: Dict[str, Tuple[str, object]]):
        self.attributes = attributes
        self.channels: List[Channel] = self.get("channels") or []
        self.compression: int = self.get("compression", NONE)
        box = self.get("dataWindow")
        if not box:
            raise ExrError("The header has no data window")
        self.xmin, self.ymin, self.xmax, self.ymax = box
        self.width = self.xmax - self.xmin + 1
        self.height = self.ymax - self.ymin + 1
        if self.width <= 0 or self.height <= 0 or self.width * self.height > 1 << 28:
            raise ExrError("The data window is %d × %d" % (self.width, self.height))
        self.display = self.get("displayWindow") or box
        self.tiles = self.get("tiles")
        kind = self.get("type")
        self.tiled = (kind in ("tiledimage",)) or (kind is None and self.tiles is not None)
        self.deep = kind in ("deepscanline", "deeptile")
        self.name = self.get("name") or ""
        self.offsets: List[int] = []

    def get(self, name: str, default=None):
        found = self.attributes.get(name)
        return default if found is None else found[1]

    @property
    def lines(self) -> int:
        return LINES.get(self.compression, 1)

    def chunk_count(self) -> int:
        given = self.get("chunkCount")
        if isinstance(given, int) and given > 0:
            return given
        if not self.tiled:
            return (self.height + self.lines - 1) // self.lines
        tx, ty, mode = self.tiles
        return _tile_count(self.width, self.height, tx, ty, mode)


def _tile_count(width: int, height: int, tx: int, ty: int, mode: int) -> int:
    """Every tile of every level, which is how long the offset table is."""
    level_mode = mode & 0xF
    rounding_up = (mode >> 4) & 1

    def levels(size: int) -> int:
        count = 1
        while size > 1:
            size = (size + 1) // 2 if rounding_up else size // 2
            count += 1
        return count

    def at(size: int, level: int) -> int:
        for _ in range(level):
            size = (size + 1) // 2 if rounding_up else size // 2
        return max(1, size)

    if level_mode == 0:
        return ((width + tx - 1) // tx) * ((height + ty - 1) // ty)
    if level_mode == 1:
        total = 0
        for level in range(levels(max(width, height))):
            w, h = at(width, level), at(height, level)
            total += ((w + tx - 1) // tx) * ((h + ty - 1) // ty)
        return total
    total = 0
    for ly in range(levels(height)):
        for lx in range(levels(width)):
            w, h = at(width, lx), at(height, ly)
            total += ((w + tx - 1) // tx) * ((h + ty - 1) // ty)
    return total


def _cstring(data: bytes, at: int) -> Tuple[str, int]:
    end = data.index(b"\0", at)
    return data[at:end].decode("latin-1"), end + 1


def _attribute(kind: str, body: bytes):
    """One attribute's value, for the types a reader might want to see."""
    try:
        if kind == "chlist":
            channels = []
            at = 0
            while at < len(body) and body[at] != 0:
                name, at = _cstring(body, at)
                pixel, linear, xs, ys = struct.unpack_from("<iB3xii", body, at)
                at += 16
                channels.append(Channel(name, pixel, bool(linear), xs, ys))
            return channels
        if kind in ("compression", "lineOrder", "envmap", "deepImageState"):
            return body[0]
        if kind == "box2i":
            return struct.unpack("<4i", body)
        if kind == "box2f":
            return struct.unpack("<4f", body)
        if kind == "int":
            return struct.unpack("<i", body)[0]
        if kind == "float":
            return struct.unpack("<f", body)[0]
        if kind == "double":
            return struct.unpack("<d", body)[0]
        if kind == "string":
            return body.decode("utf-8", "replace")
        if kind == "stringvector":
            items, at = [], 0
            while at + 4 <= len(body):
                (length,) = struct.unpack_from("<i", body, at)
                items.append(body[at + 4:at + 4 + length].decode("utf-8", "replace"))
                at += 4 + length
            return items
        if kind in ("v2i", "v2f", "v3i", "v3f", "v2d", "v3d"):
            return struct.unpack("<%d%s" % (int(kind[1]), kind[-1]), body)
        if kind in ("m33f", "m44f", "m33d", "m44d"):
            n = 9 if kind.startswith("m33") else 16
            return struct.unpack("<%d%s" % (n, kind[-1]), body)
        if kind == "tiledesc":
            return struct.unpack("<IIB", body)
        if kind == "chromaticities":
            return struct.unpack("<8f", body)
        if kind == "rational":
            return struct.unpack("<iI", body)
        if kind == "timecode":
            return struct.unpack("<II", body)
        if kind == "keycode":
            return struct.unpack("<7i", body)
        if kind == "preview":
            w, h = struct.unpack_from("<II", body)
            return (w, h, body[8:8 + w * h * 4])
    except (struct.error, ValueError):
        return None
    return None


def read_header(data: bytes, offsets: bool = True) -> Tuple[int, List[Part], int]:
    """(version flags, the parts, where the offset tables begin).

    Without [offsets] only the headers are read, which is all a description
    needs and all the first megabyte of a large file is sure to hold.
    """
    if data[:4] != MAGIC:
        raise ExrError("This is not an OpenEXR file")
    (version,) = struct.unpack_from("<I", data, 4)
    multipart = bool(version & 0x1000)
    at = 8
    parts = []
    try:
        while True:
            attributes: Dict[str, Tuple[str, object]] = {}
            while data[at] != 0:
                name, at = _cstring(data, at)
                kind, at = _cstring(data, at)
                (size,) = struct.unpack_from("<i", data, at)
                at += 4
                if size < 0 or at + size > len(data):
                    raise ExrError("The header is cut short")
                attributes[name] = (kind, _attribute(kind, data[at:at + size]))
                at += size
            at += 1
            parts.append(Part(attributes))
            if not multipart or data[at] == 0:
                break
        if multipart:
            at += 1
    except IndexError:
        raise ExrError("The header is cut short")
    if version & 0x200 and not multipart:
        # The single-part flag for a tiled file, whatever the header says.
        parts[0].tiled = True

    if not offsets:
        return version, parts, at
    for part in parts:
        count = part.chunk_count()
        end = at + 8 * count
        if end > len(data):
            raise ExrError("The file is cut short inside its offset table")
        part.offsets = list(struct.unpack_from("<%dQ" % count, data, at))
        at = end
    return version, parts, at


# -- the compressions ------------------------------------------------------

#: `d - 128`, byte for byte, for undoing the predictor with `translate`.
_MINUS_128 = bytes((i - 128) & 0xFF for i in range(256))
_BYTE = (255).__and__


def _undo_predictor(data: bytes) -> bytes:
    """ZIP's and RLE's two steps back: the byte deltas summed, and the two
    halves the bytes were split into woven together again.

    The sum is `accumulate` over the deltas, which runs in C; only taking each
    total modulo 256 is a `map` of a builtin, which does too.
    """
    n = len(data)
    if n == 0:
        return b""
    deltas = data[:1] + data[1:].translate(_MINUS_128)
    summed = bytes(map(_BYTE, accumulate(deltas)))
    out = bytearray(n)
    half = (n + 1) // 2
    out[0::2] = summed[:half]
    out[1::2] = summed[half:]
    return bytes(out)


def _unrle(data: bytes, expected: int) -> bytes:
    out = bytearray()
    at, n = 0, len(data)
    while at < n and len(out) < expected:
        count = data[at]
        if count >= 128:
            count = 256 - count
            out += data[at + 1:at + 1 + count]
            at += 1 + count
        else:
            out += data[at + 1:at + 2] * (count + 1)
            at += 2
    return bytes(out)


def _layout(part: Part, x0: int, y0: int, nx_all: int, ny_all: int):
    """Per channel: samples across and down in this block, and its bytes."""
    rows = []
    for channel in part.channels:
        nx = _samples(channel.xs, x0, x0 + nx_all - 1)
        ny = _samples(channel.ys, y0, y0 + ny_all - 1)
        rows.append((channel, nx, ny))
    return rows


def _samples(step: int, low: int, high: int) -> int:
    """How many of low..high are a multiple of [step] — a subsampled
    channel has samples only where the coordinate divides by its rate."""
    if step <= 1:
        return high - low + 1
    first = low + (-low) % step
    return 0 if first > high else (high - first) // step + 1


def _block_bytes(layout) -> int:
    return sum(nx * ny * c.size for c, nx, ny in layout)


# PXR24 --------------------------------------------------------------------


def _pxr24(data: bytes, layout, y0: int, ny_all: int) -> bytes:
    raw = zlib.decompress(data)
    out = bytearray()
    at = 0
    for y in range(y0, y0 + ny_all):
        for channel, nx, _ny in layout:
            if channel.ys > 1 and y % channel.ys:
                continue
            if channel.type == HALF:
                hi, lo = raw[at:at + nx], raw[at + nx:at + 2 * nx]
                at += 2 * nx
                sums = accumulate((h << 8) | l for h, l in zip(hi, lo))
                out += array("H", (v & 0xFFFF for v in sums)).tobytes() if LITTLE else \
                    _swapped("H", (v & 0xFFFF for v in sums))
            elif channel.type == UINT:
                p = [raw[at + k * nx:at + (k + 1) * nx] for k in range(4)]
                at += 4 * nx
                sums = accumulate((a << 24) | (b << 16) | (c << 8) | d
                                  for a, b, c, d in zip(*p))
                out += struct.pack("<%dI" % nx, *(v & 0xFFFFFFFF for v in sums))
            else:
                p = [raw[at + k * nx:at + (k + 1) * nx] for k in range(3)]
                at += 3 * nx
                sums = accumulate((a << 24) | (b << 16) | (c << 8)
                                  for a, b, c in zip(*p))
                out += struct.pack("<%dI" % nx, *(v & 0xFFFFFFFF for v in sums))
    return bytes(out)


def _swapped(code: str, values) -> bytes:
    found = array(code, values)
    found.byteswap()
    return found.tobytes()


# PIZ: Huffman -------------------------------------------------------------

_HUF_DECBITS = 14
_HUF_DECMASK = (1 << _HUF_DECBITS) - 1
_ENCSIZE = (1 << 16) + 1


def _huf_uncompress(data: bytes, count: int) -> List[int]:
    if len(data) < 20:
        raise ExrError("The PIZ Huffman block is cut short")
    im, big, _table, bits = struct.unpack_from("<IIII", data, 0)
    if im >= _ENCSIZE or big >= _ENCSIZE:
        raise ExrError("The PIZ Huffman table is out of range")

    # The code lengths, six bits each, with runs of zeros written short.
    lengths = [0] * _ENCSIZE
    c = 0
    lc = 0
    at = 20
    i = im
    while i <= big:
        c &= (1 << lc) - 1
        while lc < 6:
            c = (c << 8) | data[at]
            at += 1
            lc += 8
        lc -= 6
        length = (c >> lc) & 63
        if length == 63:
            while lc < 8:
                c = (c << 8) | data[at]
                at += 1
                lc += 8
            lc -= 8
            run = ((c >> lc) & 0xFF) + 6
            if i + run > big + 1:
                raise ExrError("The PIZ Huffman table runs past its end")
            i += run
            continue
        if length >= 59:
            run = length - 59 + 2
            if i + run > big + 1:
                raise ExrError("The PIZ Huffman table runs past its end")
            i += run
            continue
        lengths[i] = length
        i += 1

    # Canonical codes from the lengths.
    per_length = [0] * 59
    for length in lengths:
        per_length[length] += 1
    code = 0
    for length in range(58, 0, -1):
        following = (code + per_length[length]) >> 1
        per_length[length] = code
        code = following
    codes = [0] * _ENCSIZE
    for symbol in range(_ENCSIZE):
        length = lengths[symbol]
        if length:
            codes[symbol] = per_length[length]
            per_length[length] += 1

    # The table: a code of up to 14 bits fills every entry it is a prefix of;
    # a longer one is listed under its first 14 bits.
    short_len = [0] * (1 << _HUF_DECBITS)
    short_sym = [0] * (1 << _HUF_DECBITS)
    long_codes: Dict[int, List[Tuple[int, int, int]]] = {}
    for symbol in range(im, big + 1):
        length = lengths[symbol]
        if not length:
            continue
        code = codes[symbol]
        if code >> length:
            raise ExrError("The PIZ Huffman table is damaged")
        if length > _HUF_DECBITS:
            long_codes.setdefault(code >> (length - _HUF_DECBITS), []).append((length, code, symbol))
        else:
            first = code << (_HUF_DECBITS - length)
            for k in range(first, first + (1 << (_HUF_DECBITS - length))):
                short_len[k] = length
                short_sym[k] = symbol

    return _huf_decode(data, at, bits, short_len, short_sym, long_codes, big, count)


def _huf_decode(data: bytes, at: int, bits: int, short_len, short_sym, long_codes,
                rlc: int, count: int) -> List[int]:
    """The symbols, one or two table lookups each.

    A code of up to 14 bits is found in one table whose entries pack length
    and symbol into one int. A longer one is found in a second table under its
    first 14 bits, indexed by as many more bits as the longest code there
    needs — noise in a float render makes half its codes long, and walking a
    list of candidates for each was two thirds of the time. Bits are loaded
    eight bytes at a time. The run symbol repeats the value before it.
    """
    table = [(length << 17) | symbol for length, symbol in zip(short_len, short_sym)]
    subs: List[Optional[Tuple[int, List[int]]]] = [None] * (1 << _HUF_DECBITS)
    for prefix, candidates in long_codes.items():
        extra_bits = max(length for length, _c, _s in candidates) - _HUF_DECBITS
        if extra_bits > 16:
            continue  # absurdly long codes: left to the list, below
        sub = [0] * (1 << extra_bits)
        for length, code, symbol in candidates:
            rest = length - _HUF_DECBITS
            first = (code & ((1 << rest) - 1)) << (extra_bits - rest)
            sub[first:first + (1 << (extra_bits - rest))] = [(length << 17) | symbol] * (1 << (extra_bits - rest))
        subs[prefix] = (extra_bits, sub)

    out: List[int] = []
    append = out.append
    extend = out.extend
    c = 0
    lc = 0
    end = at + (bits + 7) // 8
    if end > len(data):
        raise ExrError("The PIZ Huffman data is cut short")
    shift = _HUF_DECBITS
    mask = _HUF_DECMASK
    from_bytes = int.from_bytes

    while True:
        if lc < 48 and at < end:
            take = 8 if end - at >= 8 else end - at
            c = ((c & ((1 << lc) - 1)) << (take << 3)) | from_bytes(data[at:at + take], "big")
            at += take
            lc += take << 3
        if lc < shift:
            break
        index = (c >> (lc - shift)) & mask
        entry = table[index]
        if not entry:
            second = subs[index]
            if second is not None and lc >= shift + second[0]:
                more = second[0]
                entry = second[1][(c >> (lc - shift - more)) & ((1 << more) - 1)]
            else:
                entry = _long_code(long_codes.get(index), c, lc)
            if not entry:
                raise ExrError("The PIZ Huffman data has a code no table names")
        lc -= entry >> 17
        if lc < 0:
            raise ExrError("The PIZ Huffman data ends in the middle of a code")
        symbol = entry & 0x1FFFF
        if symbol == rlc:
            if lc < 8:
                if at >= end:
                    raise ExrError("The PIZ Huffman data ends in the middle of a run")
                c = ((c & ((1 << lc) - 1)) << 8) | data[at]
                at += 1
                lc += 8
            lc -= 8
            if not out:
                raise ExrError("The PIZ data repeats a value before the first one")
            extend([out[-1]] * ((c >> lc) & 0xFF))
        else:
            append(symbol)

    # What is left: the codes in the last bits, read against zeros after them.
    extra = (8 - bits) & 7
    c = (c & ((1 << lc) - 1)) >> extra
    lc -= extra
    while lc > 0:
        entry = table[(c << (shift - lc)) & mask]
        if not entry or (entry >> 17) > lc:
            raise ExrError("The PIZ Huffman data ends in the middle of a code")
        lc -= entry >> 17
        symbol = entry & 0x1FFFF
        if symbol == rlc:
            if lc < 8:
                raise ExrError("The PIZ Huffman data ends in the middle of a run")
            lc -= 8
            extend([out[-1]] * ((c >> lc) & 0xFF))
        else:
            append(symbol)

    if len(out) != count:
        raise ExrError("The PIZ data holds %d values where %d were expected"
                       % (len(out), count))
    return out


def _long_code(candidates, c: int, lc: int) -> int:
    """A long code the second table could not answer: past 30 bits, or too
    near the end of the data for its table's width. Tried one by one."""
    for length, code, symbol in candidates or ():
        if lc >= length and (c >> (lc - length)) & ((1 << length) - 1) == code:
            return (length << 17) | symbol
    return 0


# PIZ: the wavelet ---------------------------------------------------------


def _wav2_decode(buf: List[int], start: int, nx: int, ox: int, ny: int, oy: int,
                 big: int) -> None:
    """The inverse of the 2D Haar-like wavelet, in place, level by level.

    Each level pairs samples `p` apart in both directions; a whole row of
    those quads is taken as four slices, worked as lists, and put back.
    """
    w14 = big < (1 << 14)
    n = min(nx, ny)
    p = 1
    while p <= n:
        p <<= 1
    p >>= 1
    p2 = p
    p >>= 1

    while p >= 1:
        oy1, oy2 = oy * p, oy * p2
        ox1, ox2 = ox * p, ox * p2
        across = nx // p2
        down = ny // p2
        span = across * ox2
        py = start
        for _ in range(down):
            if across:
                a = buf[py:py + span:ox2]
                b = buf[py + ox1:py + ox1 + span:ox2]
                c = buf[py + oy1:py + oy1 + span:ox2]
                d = buf[py + oy1 + ox1:py + oy1 + ox1 + span:ox2]
                a, b, c, d = _quads(a, b, c, d, w14)
                buf[py:py + span:ox2] = a
                buf[py + ox1:py + ox1 + span:ox2] = b
                buf[py + oy1:py + oy1 + span:ox2] = c
                buf[py + oy1 + ox1:py + oy1 + ox1 + span:ox2] = d
            if nx & p:
                px = py + span
                p10 = px + oy1
                buf[px], buf[p10] = _pair(buf[px], buf[p10], w14)
            py += oy2
        if ny & p:
            px = py
            for _ in range(across):
                p01 = px + ox1
                buf[px], buf[p01] = _pair(buf[px], buf[p01], w14)
                px += ox2
        p2 = p
        p >>= 1


def _pair(l: int, h: int, w14: bool) -> Tuple[int, int]:
    if w14:
        ls = ((l + 0x8000) & 0xFFFF) - 0x8000
        hs = ((h + 0x8000) & 0xFFFF) - 0x8000
        ai = ls + (hs & 1) + (hs >> 1)
        return ai & 0xFFFF, (ai - hs) & 0xFFFF
    bb = (l - (h >> 1)) & 0xFFFF
    return (h + bb - 0x8000) & 0xFFFF, bb


#: A 16-bit value read as a signed short, by table: `map` over a list's
#: `__getitem__` runs in C, where the arithmetic would run in Python.
_SIGNED = [v - 0x10000 if v & 0x8000 else v for v in range(0x10000)]
_LOW16 = (0xFFFF).__and__


def _quads(a, b, c, d, w14: bool):
    """wdec on (a, c) and (b, d), then on the two results, for a whole row.

    Written as whole-row list expressions rather than one quad at a time: the
    per-element work is then a comprehension step, and the conversions are
    `map`s of builtins. In the 14-bit mode the encoder chose it *because*
    nothing overflows, so the values stay plain ints until the end and are
    cut to 16 bits once.
    """
    if w14:
        sa = list(map(_SIGNED.__getitem__, a))
        sb = list(map(_SIGNED.__getitem__, b))
        sc = list(map(_SIGNED.__getitem__, c))
        sd = list(map(_SIGNED.__getitem__, d))
        i00 = [l + (h & 1) + (h >> 1) for l, h in zip(sa, sc)]
        i10 = [x - h for x, h in zip(i00, sc)]
        i01 = [l + (h & 1) + (h >> 1) for l, h in zip(sb, sd)]
        i11 = [x - h for x, h in zip(i01, sd)]
        oa = [l + (h & 1) + (h >> 1) for l, h in zip(i00, i01)]
        ob = [x - h for x, h in zip(oa, i01)]
        oc = [l + (h & 1) + (h >> 1) for l, h in zip(i10, i11)]
        od = [x - h for x, h in zip(oc, i11)]
        return (list(map(_LOW16, oa)), list(map(_LOW16, ob)),
                list(map(_LOW16, oc)), list(map(_LOW16, od)))
    # wdec16: b = (l - (h >> 1)) mod 2^16, a = (h + b - 2^15) mod 2^16.
    i10 = [(l - (h >> 1)) & 0xFFFF for l, h in zip(a, c)]
    i00 = [(h + bb - 0x8000) & 0xFFFF for h, bb in zip(c, i10)]
    i11 = [(l - (h >> 1)) & 0xFFFF for l, h in zip(b, d)]
    i01 = [(h + bb - 0x8000) & 0xFFFF for h, bb in zip(d, i11)]
    ob = [(l - (h >> 1)) & 0xFFFF for l, h in zip(i00, i01)]
    oa = [(h + bb - 0x8000) & 0xFFFF for h, bb in zip(i01, ob)]
    od = [(l - (h >> 1)) & 0xFFFF for l, h in zip(i10, i11)]
    oc = [(h + bb - 0x8000) & 0xFFFF for h, bb in zip(i11, od)]
    return oa, ob, oc, od


def _piz(data: bytes, layout, y0: int, ny_all: int) -> bytes:
    if len(data) < 4:
        raise ExrError("The PIZ block is cut short")
    low, high = struct.unpack_from("<HH", data, 0)
    at = 4
    bitmap = bytearray(8192)
    if high >= 8192:
        raise ExrError("The PIZ bitmap is out of range")
    if low <= high:
        bitmap[low:high + 1] = data[at:at + high - low + 1]
        at += high - low + 1

    lut = [i for i in range(65536) if i == 0 or bitmap[i >> 3] & (1 << (i & 7))]
    big = len(lut) - 1

    (length,) = struct.unpack_from("<i", data, at)
    at += 4
    total = sum(nx * ny * (c.size // 2) for c, nx, ny in layout)
    buf = _huf_uncompress(data[at:at + length], total) if total else []

    start = 0
    regions = []
    for channel, nx, ny in layout:
        size = channel.size // 2
        for j in range(size):
            _wav2_decode(buf, start + j, nx, size, ny, nx * size, big)
        regions.append((channel, start, nx * size))
        start += nx * ny * size

    values = array("H", map(lut.__getitem__, buf))
    if not LITTLE:
        values.byteswap()
    raw = values.tobytes()

    out = bytearray()
    cursor = {id(c): s * 2 for c, s, _n in regions}
    for y in range(y0, y0 + ny_all):
        for channel, _start, n in regions:
            if channel.ys > 1 and y % channel.ys:
                continue
            at = cursor[id(channel)]
            out += raw[at:at + n * 2]
            cursor[id(channel)] = at + n * 2
    return bytes(out)


# B44 ----------------------------------------------------------------------


def _unpack14(b: bytes, at: int) -> List[int]:
    s = [0] * 16
    s[0] = (b[at] << 8) | b[at + 1]
    shift = b[at + 2] >> 2
    bias = 0x20 << shift
    s[4] = (s[0] + ((((b[at + 2] << 4) | (b[at + 3] >> 4)) & 0x3F) << shift) - bias) & 0xFFFF
    s[8] = (s[4] + ((((b[at + 3] << 2) | (b[at + 4] >> 6)) & 0x3F) << shift) - bias) & 0xFFFF
    s[12] = (s[8] + ((b[at + 4] & 0x3F) << shift) - bias) & 0xFFFF
    s[1] = (s[0] + ((b[at + 5] >> 2) << shift) - bias) & 0xFFFF
    s[5] = (s[4] + ((((b[at + 5] << 4) | (b[at + 6] >> 4)) & 0x3F) << shift) - bias) & 0xFFFF
    s[9] = (s[8] + ((((b[at + 6] << 2) | (b[at + 7] >> 6)) & 0x3F) << shift) - bias) & 0xFFFF
    s[13] = (s[12] + ((b[at + 7] & 0x3F) << shift) - bias) & 0xFFFF
    s[2] = (s[1] + ((b[at + 8] >> 2) << shift) - bias) & 0xFFFF
    s[6] = (s[5] + ((((b[at + 8] << 4) | (b[at + 9] >> 4)) & 0x3F) << shift) - bias) & 0xFFFF
    s[10] = (s[9] + ((((b[at + 9] << 2) | (b[at + 10] >> 6)) & 0x3F) << shift) - bias) & 0xFFFF
    s[14] = (s[13] + ((b[at + 10] & 0x3F) << shift) - bias) & 0xFFFF
    s[3] = (s[2] + ((b[at + 11] >> 2) << shift) - bias) & 0xFFFF
    s[7] = (s[6] + ((((b[at + 11] << 4) | (b[at + 12] >> 4)) & 0x3F) << shift) - bias) & 0xFFFF
    s[11] = (s[10] + ((((b[at + 12] << 2) | (b[at + 13] >> 6)) & 0x3F) << shift) - bias) & 0xFFFF
    s[15] = (s[14] + ((b[at + 13] & 0x3F) << shift) - bias) & 0xFFFF
    return [(v & 0x7FFF) if v & 0x8000 else (~v & 0xFFFF) for v in s]


def _b44(data: bytes, layout, y0: int, ny_all: int) -> bytes:
    at = 0
    regions = []
    for channel, nx, ny in layout:
        if channel.type != HALF:
            n = nx * ny * channel.size
            regions.append((channel, data[at:at + n], nx * channel.size))
            at += n
            continue
        plane = array("H", bytes(nx * ny * 2))
        for by in range(0, ny, 4):
            for bx in range(0, nx, 4):
                if at + 3 > len(data):
                    raise ExrError("The B44 block is cut short")
                if data[at + 2] >= (13 << 2):
                    v = (data[at] << 8) | data[at + 1]
                    v = (v & 0x7FFF) if v & 0x8000 else (~v & 0xFFFF)
                    s = [v] * 16
                    at += 3
                else:
                    s = _unpack14(data, at)
                    at += 14
                for r in range(4):
                    y = by + r
                    if y >= ny:
                        break
                    row = y * nx + bx
                    take = min(4, nx - bx)
                    plane[row:row + take] = array("H", s[r * 4:r * 4 + take])
        if not LITTLE:
            plane.byteswap()
        regions.append((channel, plane.tobytes(), nx * 2))

    out = bytearray()
    cursor = {id(c): 0 for c, _d, _n in regions}
    for y in range(y0, y0 + ny_all):
        for channel, plane, n in regions:
            if channel.ys > 1 and y % channel.ys:
                continue
            k = cursor[id(channel)]
            out += plane[k:k + n]
            cursor[id(channel)] = k + n
    return bytes(out)


def decompress(part, data: bytes, x0: int, y0: int, nx_all: int, ny_all: int) -> bytes:
    """One chunk's bytes, back to the plain layout: line by line, and within
    a line channel by channel, in the order the header lists them."""
    layout = _layout(part, x0, y0, nx_all, ny_all)
    expected = _block_bytes(layout)
    kind = part.compression
    # Any compressor may store a chunk as it is when it would not shrink.
    if kind == NONE or len(data) >= expected:
        return data[:expected]
    if kind in (ZIP, ZIPS):
        return _undo_predictor(zlib.decompress(data))
    if kind == RLE:
        return _undo_predictor(_unrle(data, expected))
    if kind == PIZ:
        return _piz(data, layout, y0, ny_all)
    if kind == PXR24:
        return _pxr24(data, layout, y0, ny_all)
    if kind in (B44, B44A):
        return _b44(data, layout, y0, ny_all)
    raise ExrError("%s compression is not read here"
                   % COMPRESSION_NAMES.get(kind, "Unknown"))


# -- the picture -----------------------------------------------------------


class Image:
    """Some of a part's channels, whole or thinned.

    `planes` maps a channel's name to its samples, row after row, in the
    file's own encoding — which `types` says, per name.
    """

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.planes: Dict[str, bytes] = {}
        self.types: Dict[str, int] = {}
        self.missing_chunks = 0


#: Seconds per 16-bit value decompressed, measured on a Mac mini: PIZ works
#: its Huffman codes and wavelet one value at a time, the rest run in C.
SECONDS_PER_VALUE = {PIZ: 0.55e-6, B44: 0.4e-6, B44A: 0.4e-6, PXR24: 0.3e-6}
SECONDS_PER_VALUE_ELSE = 0.1e-6


def cost(part: Part, step: int = 1) -> float:
    """Roughly how long [read] will take at [step], in seconds, before any of
    it is done — every channel of every chunk that holds a kept row, because
    a chunk is decompressed whole whatever is wanted from it."""
    values_per_row = sum(part.width * c.size // 2 for c in part.channels)
    if part.tiled and part.tiles:
        lines = part.tiles[1]
    else:
        lines = part.lines
    blocks = (part.height + lines - 1) // lines
    kept = sum(1 for b in range(blocks)
               if any((b * lines + k) % step == 0 for k in range(min(lines, part.height - b * lines))))
    rate = SECONDS_PER_VALUE.get(part.compression, SECONDS_PER_VALUE_ELSE)
    return kept * lines * values_per_row * rate


class Spec:
    """What decompressing a chunk needs to know about its part, and no more —
    it crosses to a worker process with every batch."""

    def __init__(self, compression: int, channels: List[Channel]):
        self.compression = compression
        self.channels = channels


def decode_job(spec: Spec, job: Tuple[int, int, int, int, bytes]) -> Optional[bytes]:
    """One chunk decompressed, or None when it is damaged. Top level, so a
    worker process can be handed it."""
    x0, y0, nx, ny, raw = job
    try:
        return decompress(spec, raw, x0, y0, nx, ny)
    except (zlib.error, ExrError, IndexError, struct.error, ValueError):
        return None


def read(data: bytes, part: Part, wanted: List[str], step: int = 1, pool=None) -> Image:
    """The [wanted] channels of [part], every [step]th row and column.

    Only chunks holding a row that is kept are decompressed: a thumbnail of a
    tall ZIP image reads one chunk in [step]/16 or so. [pool], when given, is
    `pool(spec, jobs) -> blocks` and decompresses the chunks elsewhere; they
    are independent of each other, which is what makes that possible.
    """
    if part.deep:
        raise ExrError("Deep data has many samples per pixel and no single picture")
    if part.compression not in READABLE:
        raise ExrError("%s compression is not read here"
                       % COMPRESSION_NAMES.get(part.compression, "Unknown"))
    index = {c.name: i for i, c in enumerate(part.channels)}
    chosen = [part.channels[index[name]] for name in wanted if name in index]
    for channel in chosen:
        if channel.xs != 1 or channel.ys != 1:
            raise ExrError("Channel %s is subsampled, which is not drawn here" % channel.name)

    width = (part.width + step - 1) // step
    height = (part.height + step - 1) // step
    image = Image(width, height)
    planes = {c.name: bytearray(width * height * c.size) for c in chosen}
    multipart = _is_multipart(data)

    def place(block: bytes, x0: int, y0: int, nx: int, ny: int) -> None:
        # Walk the block's lines; within each, its channels in header order.
        at = 0
        for y in range(y0, y0 + ny):
            row = y - part.ymin
            keep = row % step == 0
            for channel in part.channels:
                n = _samples(channel.xs, x0, x0 + nx - 1) * channel.size
                if channel.ys > 1 and y % channel.ys:
                    continue
                if keep and channel.name in planes:
                    line = block[at:at + n]
                    size = channel.size
                    col = x0 - part.xmin
                    if step > 1:
                        line, col = _thin(line, size, col, step)
                    start = ((row // step) * width + col) * size
                    planes[channel.name][start:start + len(line)] = line
                at += n

    # First every chunk worth decompressing, then the decompressing — here,
    # or spread over the pool when there is one and the work is worth it.
    jobs: List[Tuple[int, int, int, int, bytes]] = []
    if not part.tiled:
        lines = part.lines
        for offset in part.offsets:
            head = 8 if multipart else 4
            if offset <= 0 or offset + head + 4 > len(data):
                image.missing_chunks += 1
                continue
            y, size = struct.unpack_from("<ii", data, offset + head - 4)
            ny = min(lines, part.ymax - y + 1)
            if ny <= 0 or y < part.ymin:
                image.missing_chunks += 1
                continue
            first = y - part.ymin
            if step > 1 and not any((first + k) % step == 0 for k in range(ny)):
                continue
            start = offset + head + 4
            if start + size > len(data):
                image.missing_chunks += 1
                continue
            jobs.append((part.xmin, y, part.width, ny, data[start:start + size]))
    else:
        tx, ty, _mode = part.tiles
        across = (part.width + tx - 1) // tx
        down = (part.height + ty - 1) // ty
        head = 4 if multipart else 0
        for offset in part.offsets[:across * down]:
            if offset <= 0 or offset + head + 20 > len(data):
                image.missing_chunks += 1
                continue
            cx, cy, lx, ly, size = struct.unpack_from("<5i", data, offset + head)
            if lx or ly:
                continue
            x0 = part.xmin + cx * tx
            y0 = part.ymin + cy * ty
            nx = min(tx, part.xmax - x0 + 1)
            ny = min(ty, part.ymax - y0 + 1)
            if nx <= 0 or ny <= 0:
                image.missing_chunks += 1
                continue
            if step > 1 and not any((cy * ty + k) % step == 0 for k in range(ny)):
                continue
            start = offset + head + 20
            jobs.append((x0, y0, nx, ny, data[start:start + size]))

    spec = Spec(part.compression, part.channels)
    blocks = pool(spec, jobs) if pool is not None and len(jobs) > 1 else None
    if blocks is None:
        blocks = [decode_job(spec, job) for job in jobs]
    for (x0, y0, nx, ny, _raw), block in zip(jobs, blocks):
        if block is None:
            image.missing_chunks += 1
            continue
        place(block, x0, y0, nx, ny)

    for channel in chosen:
        image.planes[channel.name] = bytes(planes[channel.name])
        image.types[channel.name] = channel.type
    return image


def _thin(line: bytes, size: int, col: int, step: int) -> Tuple[bytes, int]:
    """Every [step]th sample of a line that starts at column [col], and the
    column in the thinned picture where the first kept one lands."""
    skip = (-col) % step
    kept = bytearray()
    if size == 2:
        values = array("H", line)[skip::step]
    else:
        values = array("I", line)[skip::step]
    kept += values.tobytes()
    return bytes(kept), (col + skip) // step


def _is_multipart(data: bytes) -> bool:
    return bool(struct.unpack_from("<I", data, 4)[0] & 0x1000)
