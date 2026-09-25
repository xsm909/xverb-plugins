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

"""From light to a screen: exposure, a view transform and sRGB.

**Every sample becomes a 16-bit key, and every key has one answer.** A half
*is* 16 bits. A float's top 16 bits are its sign, its exponent and seven bits
of mantissa — a bfloat16, within 0.4% of the float, and a screen shows 256
levels. Radiance's shared exponent and a mantissa are 16 bits side by side.
So the curve is worked out once, for 65 536 keys, and a picture of any size is
then one table lookup per sample, done by `bytes.translate`'s cousin: `map`
over a builtin, in C.

**Two views.** *Standard* is what Blender, Nuke and Photoshop show by default:
the light times 2^exposure, clipped at 1, encoded for sRGB — a sky at 40 is
white. *Filmic* rolls the highlights off instead (the ACES fit Krzysztof
Narkowicz published), so the sky keeps its gradient and the midtones darken a
little.

**Data is not light.** A depth pass holds distances and a mask holds 0 and 1;
putting a curve for the eye on them shows nothing useful. A channel that is not
a colour and not luminance is stretched from its smallest value to its
largest, linearly, and the page says so.
"""

from __future__ import annotations

import math
import struct
from array import array
from typing import Dict, List, Optional, Tuple

import sys

LITTLE = sys.byteorder == "little"

#: Sample encodings, as `exr` numbers them, plus Radiance's.
UINT, HALF, FLOAT, RGBE = 0, 1, 2, 3

_values: Dict[int, Tuple[float, ...]] = {}


def values(kind: int) -> Tuple[float, ...]:
    """What each of the 65 536 keys of [kind] stands for."""
    found = _values.get(kind)
    if found is not None:
        return found
    keys = array("H", range(65536))
    if kind == HALF:
        raw = keys.tobytes() if LITTLE else _swap(keys)
        found = struct.unpack("<65536e", raw)
    elif kind == FLOAT:
        wide = array("I", (k << 16 for k in range(65536)))
        found = struct.unpack("<65536f", wide.tobytes() if LITTLE else _swap(wide))
    elif kind == RGBE:
        # Key = exponent << 8 | mantissa, the convention the reference
        # `rgbe.c` uses: (m + 0.5) / 256 * 2^(e - 128), and nothing at e = 0.
        found = tuple(
            0.0 if (k >> 8) == 0 else math.ldexp((k & 0xFF) + 0.5, (k >> 8) - 136)
            for k in range(65536)
        )
    else:
        raise ValueError(kind)
    _values[kind] = found
    return found


def _swap(values_array: array) -> bytes:
    copy = array(values_array.typecode, values_array)
    copy.byteswap()
    return copy.tobytes()


def keys(plane: bytes, kind: int) -> array:
    """A channel's samples as 16-bit keys, for [values] of the same [kind]."""
    if kind == HALF or kind == RGBE:
        found = array("H", plane)
        if not LITTLE:
            found.byteswap()
        return found
    if kind == FLOAT:
        halves = array("H", plane)
        if not LITTLE:
            halves.byteswap()
        # Little-endian: the top half of each float is the second of its two.
        return halves[1::2]
    # uint: counts, not light. Made into floats, then keys, value by value —
    # the one slow path, for a channel type that is rare in pictures.
    counts = array("I", plane)
    if not LITTLE:
        counts.byteswap()
    wide = array("f", map(float, counts))
    halves = array("H", wide.tobytes())
    return halves[1::2]


def key_kind(kind: int) -> int:
    """Which table a channel's keys are read against."""
    return HALF if kind == HALF else RGBE if kind == RGBE else FLOAT


# -- curves ----------------------------------------------------------------


def _srgb(v: float) -> float:
    if v <= 0.0031308:
        return 12.92 * v
    return 1.055 * v ** (1 / 2.4) - 0.055


def _filmic(v: float) -> float:
    v *= 0.6
    return (v * (2.51 * v + 0.03)) / (v * (2.43 * v + 0.59) + 0.14)


_tables: Dict[tuple, bytes] = {}


def table(kind: int, exposure: float, view: str) -> bytes:
    """Key → 0..255 for light: exposure, the view, sRGB."""
    marker = (kind, exposure, view)
    found = _tables.get(marker)
    if found is not None:
        return found
    gain = 2.0 ** exposure
    filmic = view == "filmic"
    out = bytearray(65536)
    for k, v in enumerate(values(kind)):
        if v != v or v <= 0.0:  # NaN, zero and below
            continue
        if v == math.inf:
            out[k] = 255
            continue
        v *= gain
        if filmic:
            v = _filmic(v)
        if v >= 1.0:
            out[k] = 255
            continue
        out[k] = int(_srgb(v) * 255.0 + 0.5)
    found = bytes(out)
    _tables[marker] = found
    return found


def stretch_table(kind: int, present: set) -> Tuple[bytes, float, float]:
    """Key → 0..255 for data: the smallest value present at 0, the largest at
    255, in a straight line. Worked out over the keys present, which is at most
    65 536 values however large the picture."""
    known = values(kind)
    finite = [known[k] for k in present if math.isfinite(known[k])]
    if not finite:
        return bytes(65536), 0.0, 0.0
    low, high = min(finite), max(finite)
    span = high - low
    out = bytearray(65536)
    if span > 0:
        for k in present:
            v = known[k]
            if not math.isfinite(v):
                out[k] = 255 if v > 0 else 0
                continue
            out[k] = int((v - low) / span * 255.0 + 0.5)
    return bytes(out), low, high


# -- a picture -------------------------------------------------------------


def to_bytes(key_array: array, lut: bytes) -> bytes:
    return bytes(map(lut.__getitem__, key_array))


def render(planes: Dict[str, bytes], types: Dict[str, int], names: List[str],
           exposure: float, view: str, data: bool = False) -> Tuple[int, bytes, Optional[Tuple[float, float]]]:
    """(channels in the picture, the pixels, the stretched range if data).

    Three names are red, green and blue; one is grey.
    """
    out_planes = []
    stretched = None
    for name in names:
        kind = key_kind(types[name])
        found = keys(planes[name], types[name])
        if data:
            lut, low, high = stretch_table(kind, set(found))
            stretched = (low, high)
        else:
            lut = table(kind, exposure, view)
        out_planes.append(to_bytes(found, lut))

    if len(out_planes) == 1:
        return 1, out_planes[0], stretched
    n = len(out_planes[0])
    rgb = bytearray(n * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = out_planes[0], out_planes[1], out_planes[2]
    return 3, bytes(rgb), stretched
