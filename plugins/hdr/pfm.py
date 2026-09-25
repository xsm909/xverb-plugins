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

"""Portable float map: three lines of text and then floats.

`PF` for colour, `Pf` for grey, the size, and a scale whose sign is the byte
order — negative for little-endian. Rows run bottom to top. It is what
denoisers and research code write when they want floats and no library.
"""

from __future__ import annotations

from array import array
from typing import Dict, Tuple

import sys


class PfmError(Exception):
    pass


def read_header(data: bytes) -> Tuple[int, int, int, bool, int]:
    """(channels, width, height, little-endian, where the floats begin)."""
    fields = []
    at = 0
    while len(fields) < 4:
        while at < len(data) and data[at:at + 1].isspace():
            at += 1
        start = at
        while at < len(data) and not data[at:at + 1].isspace():
            at += 1
        if start == at:
            raise PfmError("The header is cut short")
        fields.append(data[start:at].decode("latin-1"))
    at += 1  # the single whitespace byte after the scale
    if fields[0] not in ("PF", "Pf"):
        raise PfmError("This is not a portable float map")
    try:
        width, height, scale = int(fields[1]), int(fields[2]), float(fields[3])
    except ValueError:
        raise PfmError("The header's numbers are not numbers")
    if width <= 0 or height <= 0 or width * height > 1 << 28:
        raise PfmError("The picture is %d × %d" % (width, height))
    return (3 if fields[0] == "PF" else 1), width, height, scale < 0, at


def read(data: bytes, step: int = 1):
    """(width, height, {name: floats as little-endian bytes})."""
    channels, width, height, little, at = read_header(data)
    need = width * height * channels * 4
    if at + need > len(data):
        raise PfmError("The file holds %d bytes of the %d its size calls for"
                       % (len(data) - at, need))
    floats = array("f", data[at:at + need])
    if little != (sys.byteorder == "little"):
        floats.byteswap()

    out_w = (width + step - 1) // step
    out_h = (height + step - 1) // step
    names = ["R", "G", "B"] if channels == 3 else ["Y"]
    planes: Dict[str, bytearray] = {n: bytearray() for n in names}
    stride = width * channels
    for out_y in range(out_h):
        y = height - 1 - out_y * step  # bottom to top in the file
        row = floats[y * stride:(y + 1) * stride]
        for c, name in enumerate(names):
            picked = row[c::channels][::step]
            if sys.byteorder != "little":
                picked.byteswap()
            planes[name] += picked.tobytes()
    return out_w, out_h, {n: bytes(p) for n, p in planes.items()}
