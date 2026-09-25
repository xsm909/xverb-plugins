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

"""Radiance `.hdr`: the environment maps a 3D scene is lit with.

Greg Ward's format from 1985 and still the one HDRI sites hand out: a text
header, a line giving the size, and pixels of four bytes — a mantissa each for
red, green and blue and one exponent they share. Scan lines are run-length
coded one component at a time, which is the form every writer since 1991 has
used; the flat form and the older run form are read too.

What comes back is three planes of 16-bit keys, exponent over mantissa, which
`tone.py` has a table for.
"""

from __future__ import annotations

from typing import Dict, List, Tuple


class RgbeError(Exception):
    pass


def read_header(data: bytes) -> Tuple[Dict[str, str], List[str], int, int, bool, int]:
    """(variables, other header lines, width, height, bottom-up, where the
    pixels begin)."""
    if not (data.startswith(b"#?") or data.startswith(b"FORMAT=")):
        raise RgbeError("This is not a Radiance picture")
    at = 0
    variables: Dict[str, str] = {}
    other: List[str] = []
    while True:
        end = data.find(b"\n", at)
        if end < 0:
            raise RgbeError("The header never ends")
        line = data[at:end].decode("latin-1").strip()
        at = end + 1
        if not line:
            break
        if line.startswith("#"):
            if not line.startswith("#?"):
                other.append(line[1:].strip())
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            variables[key.strip().upper()] = value.strip()
        else:
            other.append(line)
    end = data.find(b"\n", at)
    size = data[at:end].decode("latin-1").split()
    at = end + 1
    if len(size) != 4:
        raise RgbeError("The size line is not one this reader understands")
    if size[0] in ("-Y", "+Y") and size[2] == "+X":
        height, width = int(size[1]), int(size[3])
        bottom_up = size[0] == "+Y"
    else:
        raise RgbeError("A picture stored %s is turned; only upright ones are read"
                        % " ".join(size))
    if width <= 0 or height <= 0 or width * height > 1 << 28:
        raise RgbeError("The picture is %d × %d" % (width, height))
    return variables, other, width, height, bottom_up, at


def read(data: bytes, step: int = 1):
    """(width, height, {"R","G","B": keys as little-endian 16-bit}, header).

    Every [step]th row and column. RLE has to be walked row by row whatever is
    kept, so a thumbnail saves the arithmetic after it and not the walk.
    """
    variables, other, width, height, bottom_up, at = read_header(data)
    fmt = variables.get("FORMAT", "32-bit_rle_rgbe")
    out_w = (width + step - 1) // step
    out_h = (height + step - 1) // step
    comps = [bytearray(out_w * out_h) for _ in range(4)]
    n = len(data)

    for y in range(height):
        row = [bytearray(width) for _ in range(4)]
        if at + 4 <= n and data[at] == 2 and data[at + 1] == 2 and \
                ((data[at + 2] << 8) | data[at + 3]) == width and 8 <= width < 0x8000:
            at += 4
            for c in range(4):
                x = 0
                target = row[c]
                while x < width:
                    if at >= n:
                        raise RgbeError("The picture is cut short at row %d" % y)
                    count = data[at]
                    if count > 128:
                        count -= 128
                        target[x:x + count] = data[at + 1:at + 2] * count
                        at += 2
                    else:
                        if count == 0:
                            raise RgbeError("A run of nothing at row %d" % y)
                        target[x:x + count] = data[at + 1:at + 1 + count]
                        at += 1 + count
                    x += count
        else:
            at = _flat_row(data, at, width, row)

        out_y, kept = divmod(y if not bottom_up else height - 1 - y, step)
        if kept:
            continue
        base = out_y * out_w
        for c in range(4):
            comps[c][base:base + out_w] = row[c][::step]

    planes = {}
    for name, c in (("R", 0), ("G", 1), ("B", 2)):
        keys = bytearray(out_w * out_h * 2)
        keys[0::2] = comps[c]
        keys[1::2] = comps[3]
        planes[name] = bytes(keys)
    return out_w, out_h, planes, {"variables": variables, "other": other,
                                  "format": fmt, "width": width, "height": height}


def _flat_row(data: bytes, at: int, width: int, row) -> int:
    """A row stored flat, or with the old runs: a pixel of 1, 1, 1 repeats
    the one before it, a count that grows by eight bits each time in a row."""
    x = 0
    shift = 0
    n = len(data)
    while x < width:
        if at + 4 > n:
            raise RgbeError("The picture is cut short")
        r, g, b, e = data[at], data[at + 1], data[at + 2], data[at + 3]
        at += 4
        if r == 1 and g == 1 and b == 1 and x > 0:
            count = e << shift
            for c in range(4):
                row[c][x:x + count] = bytes([row[c][x - 1]]) * count
            x += count
            shift += 8
            continue
        row[0][x], row[1][x], row[2][x], row[3][x] = r, g, b, e
        x += 1
        shift = 0
    return at
