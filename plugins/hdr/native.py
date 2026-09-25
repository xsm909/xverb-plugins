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

"""tinyexr, compiled, for the machines with no EXR decoder of their own.

**Why a library in a plugin of Python.** Windows and Linux have nothing like
macOS's ImageIO, and Python decodes PIZ a value at a time: a 4K HDRI took 26
seconds on one core and 8 on all of them. tinyexr is OpenEXR's formats in
C++, BSD-licensed; `native/src` holds it with the wrapper that gives it one
function to call (`xvexr.cc`), and `native/build.sh` builds it with zig for Windows x64 and
Linux x64 and arm64. `ctypes` loads it — standard library — and lets go of
the interpreter's lock for the call, so it runs on its own threads.

**The plugin still decides everything.** The header is read here, in Python,
and so is the choice of part and channels; the library is told which, and
answers with float planes in the shape `exr.read` gives. Exposure and the view
come after, the same for all three paths.

**Nothing depends on it.** No library for this machine, one that will not
load, an older interface, a file it refuses: each is `None`, and the plugin
reads the file itself. `XVERB_HDR_NATIVE` names a library to use instead —
for trying a build on a machine it is not shipped for.
"""

from __future__ import annotations

import ctypes
import os
import platform
import sys
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
INTERFACE = 1

_lib = None


def target() -> Optional[str]:
    """This machine's folder under `native/`, as build.sh names them."""
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if arch is None:
        return None
    if os.name == "nt":
        return "windows-" + arch
    if sys.platform.startswith("linux"):
        return "linux-" + arch
    if sys.platform == "darwin":
        return "macos-" + arch
    return None


def _path() -> Optional[str]:
    given = os.environ.get("XVERB_HDR_NATIVE")
    if given:
        return given
    folder = target()
    if folder is None:
        return None
    name = {"windows": "xvexr.dll", "linux": "libxvexr.so", "macos": "libxvexr.dylib"}[folder.split("-")[0]]
    return os.path.join(HERE, "native", folder, name)


def _load():
    global _lib
    if _lib is not None:
        return _lib or None
    _lib = False
    path = _path()
    if not path or not os.path.isfile(path):
        return None
    try:
        lib = ctypes.CDLL(path)
        lib.xv_version.restype = ctypes.c_int
        if lib.xv_version() != INTERFACE:
            return None
        lib.xv_decode.restype = ctypes.c_int
        lib.xv_decode.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int,
            ctypes.POINTER(ctypes.c_char_p), ctypes.c_int,
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.c_char_p, ctypes.c_int,
        ]
    except (OSError, AttributeError):
        return None
    _lib = lib
    return lib


def available() -> bool:
    return _load() is not None


last_error = ""


def decode(raw: bytes, part: int, names: List[str], width: int, height: int) -> Optional[Dict[str, bytes]]:
    """{name: float32 samples, little-endian, row after row}, or None."""
    global last_error
    last_error = ""
    lib = _load()
    if lib is None:
        return None
    count = len(names)
    plane = width * height
    out = (ctypes.c_float * (plane * count))()
    wanted = (ctypes.c_char_p * count)(*[n.encode("utf-8") for n in names])
    err = ctypes.create_string_buffer(512)
    done = lib.xv_decode(raw, len(raw), part, wanted, count, out, width, height, err, len(err))
    if done != 0:
        last_error = err.value.decode("utf-8", "replace")
        return None
    body = memoryview(out).cast("B")
    size = plane * 4
    return {name: bytes(body[k * size:(k + 1) * size]) for k, name in enumerate(names)}
