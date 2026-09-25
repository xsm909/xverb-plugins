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

"""Eight-bit pixels as a PNG the host can decode.

Grey or RGB — a picture made from light is shown without its alpha, and three
bytes a pixel is a quarter less to pack than four. RGBA only for the small
preview an EXR may carry, which is eight-bit already. Rows go unfiltered:
a filter is arithmetic per byte, which is seconds in Python at this size.
"""

from __future__ import annotations

import struct
import zlib


def write(width: int, height: int, channels: int, pixels: bytes, level: int = 6) -> bytes:
    stride = width * channels
    raw = bytearray()
    for row in range(height):
        raw += b"\x00"
        raw += pixels[row * stride:(row + 1) * stride]
    colour = {1: 0, 3: 2, 4: 6}[channels]
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), level))
        + _chunk(b"IEND", b"")
    )


def _chunk(kind: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    )
