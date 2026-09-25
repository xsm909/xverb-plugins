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

"""The system's own EXR decoder, on the machine that has one.

**Why.** macOS reads OpenEXR through ImageIO, which is the OpenEXR library in
C++. A 4K HDRI in PIZ that takes this plugin's Python eight seconds on every
core takes ImageIO 0.3 seconds on one. `ctypes` is in the standard library,
and ImageIO, CoreGraphics and CoreFoundation are on every Mac, so nothing is
shipped for it.

**Only the decoding is borrowed.** The pixels are taken from the image's own
data provider, as the file stores them — half or float, unpremultiplied,
unconverted — and not drawn into a bitmap, which multiplies colour by alpha
and clips to a colour space. Measured against the fixtures' source values:
float exact, half to its own rounding. Exposure, the view and the choice of
layer stay this plugin's, so the picture is the same one either way.

**Where it is not asked.** A file of several parts or layers, where ImageIO
picks a layer by rules of its own; anything but plain R, G and B; and
anything it refuses — DWAA and DWAB among them on this machine, measured
2026-09-25, whatever one might expect of it. Then the plugin reads the file
itself, as it does everywhere else.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Dict, Optional, Tuple

_lib = None


def _load():
    global _lib
    if _lib is not None:
        return _lib or None
    if sys.platform != "darwin":
        _lib = False
        return None
    try:
        v = ctypes.c_void_p
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        io = ctypes.CDLL("/System/Library/Frameworks/ImageIO.framework/ImageIO")
        cf.CFDataCreateWithBytesNoCopy.restype = v
        cf.CFDataCreateWithBytesNoCopy.argtypes = [v, ctypes.c_char_p, ctypes.c_long, v]
        cf.CFDataGetLength.restype = ctypes.c_long
        cf.CFDataGetLength.argtypes = [v]
        cf.CFDataGetBytePtr.restype = v
        cf.CFDataGetBytePtr.argtypes = [v]
        cf.CFRelease.argtypes = [v]
        io.CGImageSourceCreateWithData.restype = v
        io.CGImageSourceCreateWithData.argtypes = [v, v]
        io.CGImageSourceCreateImageAtIndex.restype = v
        io.CGImageSourceCreateImageAtIndex.argtypes = [v, ctypes.c_size_t, v]
        for name, kind in (("CGImageGetWidth", ctypes.c_size_t), ("CGImageGetHeight", ctypes.c_size_t),
                           ("CGImageGetBitsPerComponent", ctypes.c_size_t),
                           ("CGImageGetBitsPerPixel", ctypes.c_size_t),
                           ("CGImageGetBytesPerRow", ctypes.c_size_t),
                           ("CGImageGetBitmapInfo", ctypes.c_uint32),
                           ("CGImageGetDataProvider", v)):
            getattr(cg, name).restype = kind
            getattr(cg, name).argtypes = [v]
        cg.CGDataProviderCopyData.restype = v
        cg.CGDataProviderCopyData.argtypes = [v]
        null = v.in_dll(cf, "kCFAllocatorNull")
        _lib = (cf, cg, io, null)
    except (OSError, AttributeError, ValueError):
        _lib = False
        return None
    return _lib


#: kCGBitmapFloatComponents, and the byte-order field, in a bitmap info.
FLOAT_COMPONENTS = 1 << 8
BYTE_ORDER_MASK = 0x7000
LITTLE_16, LITTLE_32 = 1 << 12, 2 << 12


def decode(raw: bytes) -> Optional[Tuple[int, int, int, Dict[str, bytes]]]:
    """(width, height, sample type, {"R","G","B": samples}), or None.

    The sample type is `tone.HALF` or `tone.FLOAT`, and each plane is that
    channel's samples row after row, little-endian — the same shape `exr.read`
    gives, so everything after this is shared.
    """
    lib = _load()
    if lib is None:
        return None
    cf, cg, io, null = lib
    held = []
    try:
        data = cf.CFDataCreateWithBytesNoCopy(None, raw, len(raw), null)
        if not data:
            return None
        held.append(data)
        source = io.CGImageSourceCreateWithData(data, None)
        if not source:
            return None
        held.append(source)
        image = io.CGImageSourceCreateImageAtIndex(source, 0, None)
        if not image:
            return None
        held.append(image)

        width, height = cg.CGImageGetWidth(image), cg.CGImageGetHeight(image)
        bpc, bpp = cg.CGImageGetBitsPerComponent(image), cg.CGImageGetBitsPerPixel(image)
        stride, info = cg.CGImageGetBytesPerRow(image), cg.CGImageGetBitmapInfo(image)
        order = info & BYTE_ORDER_MASK
        # Only what was measured: float components, 16- or 32-bit, four to a
        # pixel, little-endian. Anything else is somebody else's layout.
        if not info & FLOAT_COMPONENTS or bpc not in (16, 32) or bpp != bpc * 4:
            return None
        if order not in (0, LITTLE_16 if bpc == 16 else LITTLE_32) or sys.byteorder != "little":
            return None

        pixels = cg.CGDataProviderCopyData(cg.CGImageGetDataProvider(image))
        if not pixels:
            return None
        held.append(pixels)
        size = cf.CFDataGetLength(pixels)
        if size < stride * height:
            return None
        body = ctypes.string_at(cf.CFDataGetBytePtr(pixels), size)
    except Exception:  # noqa: BLE001 - the plugin's own reader is the answer
        return None
    finally:
        for thing in reversed(held):
            cf.CFRelease(thing)

    sample = bpc // 8
    row = width * 4 * sample
    if stride != row:
        body = b"".join(body[y * stride:y * stride + row] for y in range(height))
    planes = {}
    for index, name in enumerate("RGB"):
        plane = bytearray(width * height * sample)
        for byte in range(sample):
            plane[byte::sample] = body[index * sample + byte::4 * sample]
        planes[name] = bytes(plane)
    return width, height, (1 if bpc == 16 else 2), planes
