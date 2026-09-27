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

"""A mesh out of an Unreal package: the editor's `MeshDescription`.

**Where it is.** An editor package keeps each LOD's source mesh as a
`FCompressedBuffer` — a big-endian header (`0xb7756362`), block sizes, then
the blocks — usually Oodle-compressed, sometimes LZ4 or stored. Rather than
walk the object that owns it, the buffers are found by their magic and the one
that reads as a mesh description is taken.

**Oodle is not ours to ship.** Epic's decompressor is closed, and every
Unreal install carries it as a shared library; `oodle()` looks for one there
and loads it. Without an install the thumbnail is all there is to show — and
the page says why.

**The description** is a map of element types — `Vertices`,
`VertexInstances`, `Edges`, `Triangles`, `Polygons`, `PolygonGroups` — each
a sparse array (a bit mask of which ids are in use) and a set of attributes.
An attribute is a name, a type (`FVector4f`, `FVector3f`, `FVector2f`,
`float`, `int32`, `bool`, `FName`…), how many values an element has, then per
channel a bulk array, then its default and flags. A triangle names three
vertex instances; an instance names its vertex (the position) and carries the
normal and the UVs.
"""

from __future__ import annotations

import ctypes
import glob
import os
import struct
import sys
from typing import Dict, List, Optional, Tuple

MAGIC = struct.pack(">I", 0xB7756362)

#: The attribute types by the index the description writes: the struct format of
#: one value, or a word for the two that are not plain numbers.
_TYPES = {0: ("f", 4), 1: ("f", 3), 2: ("f", 2), 3: ("f", 1), 4: ("i", 1), 5: ("?", 1),
          6: "name", 7: "transform"}


class MeshError(Exception):
    pass


# -- Oodle, from an Unreal install ---------------------------------------------------

_LIBRARY = {"darwin": "liboo2coremac64*.dylib", "win32": "oo2core_*_win64.dll",
            "linux": "liboo2corelinux64.so*"}

_oodle = None
_looked = False


def _roots() -> List[str]:
    home = os.path.expanduser("~")
    guesses = [os.path.join(home, "Games", "UE_*"), "/Users/Shared/Epic Games/UE_*",
               "/Applications/Epic Games/UE_*", "C:/Program Files/Epic Games/UE_*",
               "D:/Program Files/Epic Games/UE_*", "D:/Epic Games/UE_*",
               os.path.join(home, "UnrealEngine*"), os.path.join(home, "Epic", "UE_*"),
               os.path.join(home, "UE_*")]
    found = []
    for guess in guesses:
        found.extend(sorted(glob.glob(guess), reverse=True))
    # The launcher's own list of what it installed, where it keeps one.
    for listing in (os.path.join(home, "Library/Application Support/Epic/UnrealEngineLauncher/"
                                       "LauncherInstalled.dat"),
                    "C:/ProgramData/Epic/UnrealEngineLauncher/LauncherInstalled.dat"):
        try:
            import json
            with open(listing, encoding="utf-8") as handle:
                for entry in json.load(handle).get("InstallationList", []):
                    where = entry.get("InstallLocation")
                    if where and os.path.isdir(os.path.join(where, "Engine")):
                        found.append(where)
        except (OSError, ValueError):
            pass
    return found


def oodle_library(extra: str = "") -> Optional[str]:
    """The path of an Oodle decompressor in an Unreal install, or None."""
    pattern = next((v for k, v in _LIBRARY.items() if sys.platform.startswith(k)), None)
    if pattern is None:
        return None
    places = []
    if extra:
        places.append(extra)
    for root in _roots():
        engine = os.path.join(root, "Engine")
        places += [os.path.join(engine, "Binaries", "DotNET", "AutomationTool"),
                   os.path.join(engine, "Binaries", "DotNET", "UnrealBuildTool"),
                   os.path.join(engine, "Binaries", "ThirdParty", "Oodle", "*"),
                   os.path.join(engine, "Source", "Runtime", "OodleDataCompression", "Sdks",
                                "*", "lib", "*")]
    for place in places:
        if os.path.isfile(place):
            return place
        for candidate in sorted(glob.glob(os.path.join(place, pattern)), reverse=True):
            if "_dbg" not in candidate and os.path.isfile(candidate):
                return candidate
    return None


def oodle(extra: str = ""):
    """`OodleLZ_Decompress`, loaded once, or None when no install has it."""
    global _oodle, _looked
    if _looked:
        return _oodle
    _looked = True
    path = oodle_library(extra)
    if path is None:
        return None
    try:
        library = ctypes.CDLL(path)
        function = library.OodleLZ_Decompress
    except (OSError, AttributeError):
        return None
    function.restype = ctypes.c_ssize_t
    function.argtypes = [ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_ssize_t,
                         ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                         ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_ssize_t, ctypes.c_int]
    _oodle = function
    return _oodle


def _lz4_block(src: bytes) -> bytes:
    out = bytearray()
    at, end = 0, len(src)
    while at < end:
        token = src[at]
        at += 1
        literal = token >> 4
        if literal == 15:
            while True:
                more = src[at]
                at += 1
                literal += more
                if more != 255:
                    break
        out += src[at:at + literal]
        at += literal
        if at >= end:
            break
        offset = src[at] | (src[at + 1] << 8)
        at += 2
        length = token & 15
        if length == 15:
            while True:
                more = src[at]
                at += 1
                length += more
                if more != 255:
                    break
        length += 4
        start = len(out) - offset
        if start < 0 or offset == 0:
            raise MeshError("A damaged LZ4 block.")
        if offset >= length:
            out += out[start:start + length]
        else:
            piece = out[start:]
            whole, rest = divmod(length, offset)
            out += piece * whole + piece[:rest]
    return bytes(out)


# -- compressed buffers --------------------------------------------------------------

class NeedsOodle(MeshError):
    pass


def buffers(data: bytes):
    """Every `FCompressedBuffer` in the package: ``(at, method, block exponent,
    block sizes, raw size)``."""
    at = data.find(MAGIC)
    while at != -1:
        if at + 64 <= len(data):
            _, _, method, _, _, exponent, blocks, raw, _ = struct.unpack_from(">IIBBBBIQQ", data, at)
            if method in (0, 3, 4) and blocks < 1 << 20 and raw < 1 << 34:
                sizes = struct.unpack_from(">%dI" % blocks, data, at + 64) if method else ()
                yield at, method, exponent, sizes, raw
        at = data.find(MAGIC, at + 4)


def decompress(data: bytes, at: int, method: int, exponent: int, sizes, raw: int) -> bytes:
    if method == 0:
        return data[at + 64:at + 64 + raw]
    if method == 3 and oodle() is None:
        raise NeedsOodle("Oodle")
    out = bytearray()
    pos = at + 64 + 4 * len(sizes)
    left = raw
    block = 1 << exponent
    for size in sizes:
        want = min(block, left)
        src = data[pos:pos + size]
        if size == want:
            out += src
        elif method == 4:
            out += _lz4_block(src)
        else:
            target = ctypes.create_string_buffer(want)
            got = _oodle(src, size, target, want, 1, 0, 0, None, 0, None, None, None, 0, 3)
            if got != want:
                raise MeshError("An Oodle block did not decompress.")
            out += target.raw
        pos += size
        left -= want
    return bytes(out)


# -- the description -----------------------------------------------------------------

class _R:
    __slots__ = ("d", "at")

    def __init__(self, data: bytes, at: int = 0):
        self.d = data
        self.at = at

    def i32(self) -> int:
        if self.at + 4 > len(self.d):
            raise MeshError("The mesh description ends early.")
        v = struct.unpack_from("<i", self.d, self.at)[0]
        self.at += 4
        return v

    def string(self) -> str:
        n = self.i32()
        if n == 0:
            return ""
        if n < 0:
            n = -n
            text = self.d[self.at:self.at + n * 2].decode("utf-16-le", "replace")[:-1]
            self.at += n * 2
            return text
        if n > 4096:
            raise MeshError("Not a mesh description.")
        text = self.d[self.at:self.at + n - 1].decode("latin-1")
        self.at += n
        return text


class Attribute:
    __slots__ = ("type", "extent", "channels", "default")

    def __init__(self, type_, extent, channels, default):
        self.type = type_
        self.extent = extent
        #: Per channel: the values, flattened, `extent` × components each.
        self.channels = channels
        self.default = default


def _value(r: _R, spec):
    """One value, the way the archive writes a single one."""
    if spec == "name":
        return r.string()
    if spec == "transform":
        # FTransform: rotation, translation, scale — doubles since UE 5.0.
        values = struct.unpack_from("<10d", r.d, r.at)
        r.at += 80
        return (values[0:4], values[4:7], values[7:10])
    fmt, width = spec
    size = 4 if fmt == "?" else struct.calcsize("<%d%s" % (width, fmt))
    raw = r.d[r.at:r.at + size]
    r.at += size
    return raw


def _unbounded(r: _R, spec) -> List[list]:
    """`TAttributeArrayContainer`: per element, a list of values."""
    if spec in ("name", "transform"):
        raise MeshError("An unbounded attribute of names.")
    fmt, width = spec
    out: List[list] = []
    chunks = r.i32()
    if not 0 <= chunks < 1 << 20:
        raise MeshError("Not a mesh description.")
    for _ in range(chunks):
        count = r.i32()
        size = 1 if fmt == "?" else struct.calcsize("<%d%s" % (width, fmt))
        if count < 0 or r.at + count * size > len(r.d):
            raise MeshError("An attribute runs past the description.")
        data = struct.unpack_from("<%d%s" % (count * width, fmt), r.d, r.at) if fmt != "?" \
            else tuple(r.d[r.at:r.at + count])
        r.at += count * size
        elements = r.i32()
        starts = struct.unpack_from("<%di" % elements, r.d, r.at)
        r.at += 4 * elements
        counts = struct.unpack_from("<%di" % elements, r.d, r.at)
        r.at += 4 * elements
        r.at += 4 * elements  # the room each element has
        for start, n in zip(starts, counts):
            out.append(list(data[start * width:(start + n) * width]))
    r.i32()  # elements
    _value(r, spec)
    return out


def _attributes(r: _R) -> Dict[str, Attribute]:
    r.i32()  # the elements the set is sized for
    count = r.i32()
    if not 0 <= count < 256:
        raise MeshError("Not a mesh description.")
    out: Dict[str, Attribute] = {}
    for _ in range(count):
        name = r.string()
        kind = r.i32()
        extent = r.i32()
        r.i32()  # elements
        channels_count = r.i32()
        if not 0 <= channels_count < 64:
            raise MeshError("Not a mesh description.")
        spec = _TYPES.get(kind)
        if spec is None:
            raise MeshError("An attribute of a type this cannot read (%d)." % kind)
        if extent == 0:
            # Unbounded: each element its own number of values — a vertex's
            # skin weights. Kept in chunks of 256 elements.
            channels = [_unbounded(r, spec) for _ in range(channels_count)]
            _value(r, spec)
            r.i32()  # flags
            out[name.strip()] = Attribute(kind, extent, channels, None)
            continue
        channels = []
        for _ in range(channels_count):
            r.i32()  # extent again
            if spec == "transform":
                channels.append([_value(r, spec) for _ in range(r.i32())])
                continue
            if spec == "name":
                channels.append([r.string() for _ in range(r.i32())])
                continue
            element = r.i32()
            n = r.i32()
            if n < 0 or element < 0 or r.at + element * n > len(r.d):
                raise MeshError("An attribute runs past the description.")
            fmt, width = spec
            if fmt == "?":
                values = list(r.d[r.at:r.at + n])
            else:
                values = list(struct.unpack_from("<%d%s" % (n * element // 4, fmt), r.d, r.at))
            r.at += element * n
            channels.append(values)
        if spec in ("name", "transform"):
            default = _value(r, spec)
        else:
            fmt, width = spec
            size = 4 if fmt == "?" else struct.calcsize("<%d%s" % (width, fmt))
            default = r.d[r.at:r.at + size]
            r.at += size
        r.i32()  # flags
        # The engine declares the position as "Position ", space and all.
        out[name.strip()] = Attribute(kind, extent, channels, default)
    return out


def description(data: bytes) -> Dict[str, Tuple[List[int], Dict[str, Attribute]]]:
    """Element type → (ids in use, attributes)."""
    r = _R(data)
    types = r.i32()
    if not 0 < types < 32:
        raise MeshError("Not a mesh description.")
    out = {}
    for _ in range(types):
        name = r.string()
        channels = r.i32()
        if not 0 <= channels < 8:
            raise MeshError("Not a mesh description.")
        for _ in range(channels):
            bits = r.i32()
            if not 0 <= bits < 1 << 28:
                raise MeshError("Not a mesh description.")
            words = struct.unpack_from("<%dI" % ((bits + 31) // 32), data, r.at)
            r.at += 4 * len(words)
            r.i32()
            used = [i for i in range(bits) if words[i >> 5] >> (i & 31) & 1]
            out[name] = (used, _attributes(r))
    if "Triangles" not in out or "Vertices" not in out:
        raise MeshError("Not a mesh description.")
    return out


def descriptions(data: bytes) -> List[Dict]:
    """Every mesh description in the package, in file order."""
    found = []
    for buffer in buffers(data):
        try:
            payload = decompress(data, *buffer)
        except NeedsOodle:
            raise
        except MeshError:
            continue
        if len(payload) < 16:
            continue
        try:
            found.append(description(payload))
        except (MeshError, struct.error):
            continue
    return found


# -- triangles ------------------------------------------------------------------------

def parts(md: Dict) -> Tuple[List[dict], int]:
    """The mesh as the 3D view's parts — one per material slot — and its triangles.

    Y and Z swapped: Unreal is left-handed with Z up, the view right-handed
    with Y up, and the swap is both the quarter turn and the mirror. Being a
    mirror, it also turns Unreal's clockwise front faces into the view's
    counter-clockwise ones, so the corners keep their order.
    """
    _, vertex_attrs = md["Vertices"]
    positions = vertex_attrs["Position"].channels[0]
    _, instance_attrs = md.get("VertexInstances", ([], {}))
    instance_vertex = instance_attrs["VertexIndex"].channels[0]
    normals = instance_attrs.get("Normal")
    normals = normals.channels[0] if normals and normals.channels else None
    uv = instance_attrs.get("TextureCoordinate")
    uvs = uv.channels[0] if uv and uv.channels else None
    triangle_ids, triangle_attrs = md["Triangles"]
    corners = triangle_attrs["VertexInstanceIndex"].channels[0]
    groups = triangle_attrs.get("PolygonGroupIndex")
    groups = groups.channels[0] if groups and groups.channels else None
    group_ids, group_attrs = md.get("PolygonGroups", ([], {}))
    slot_names = group_attrs.get("ImportedMaterialSlotName")
    slot_names = slot_names.channels[0] if slot_names and slot_names.channels else []

    by_group: Dict[int, List[int]] = {}
    for t in triangle_ids:
        g = groups[t] if groups and t < len(groups) else 0
        by_group.setdefault(g, []).append(t)

    out = []
    total = 0
    for g, triangles in sorted(by_group.items()):
        pos: List[float] = []
        nor: List[float] = []
        tex: List[float] = []
        index: List[int] = []
        sources: List[int] = []
        seen: Dict[int, int] = {}
        for t in triangles:
            three = corners[t * 3:t * 3 + 3]
            if len(three) < 3:
                continue
            made = []
            for instance in three:
                slot = seen.get(instance)
                if slot is None:
                    vertex = instance_vertex[instance]
                    x, y, z = positions[vertex * 3:vertex * 3 + 3]
                    slot = len(pos) // 3
                    seen[instance] = slot
                    sources.append(vertex)
                    pos.extend((x, z, y))
                    if normals is not None:
                        nx, ny, nz = normals[instance * 3:instance * 3 + 3]
                        nor.extend((nx, nz, ny))
                    else:
                        nor.extend((0.0, 1.0, 0.0))
                    if uvs is not None:
                        tex.extend(uvs[instance * 2:instance * 2 + 2])
                    else:
                        tex.extend((0.0, 0.0))
                made.append(slot)
            index.extend(made)
        total += len(index) // 3
        name = slot_names[g] if g < len(slot_names) else "Material %d" % g
        out.append({"name": name, "positions": pos, "normals": nor, "uvs": tex, "indices": index,
                    "sources": sources})
    return out, total


# -- a skin -------------------------------------------------------------------------

MAX_INFLUENCES = 4


def bones(md: Dict) -> Optional[Tuple[List[str], List[int], List[tuple]]]:
    """A skeletal mesh's own bones: names, parents, and each one's rest
    transform local to its parent — or None for a mesh without bones."""
    found = md.get("BonesElementName") or md.get("Bones")
    if not found:
        return None
    _, attrs = found
    names = attrs["Name"].channels[0] if "Name" in attrs else []
    parents = attrs["ParentIndex"].channels[0] if "ParentIndex" in attrs else []
    pose = attrs["Pose"].channels[0] if "Pose" in attrs else []
    if not names or len(parents) != len(names) or len(pose) != len(names):
        return None
    return list(names), list(parents), list(pose)


def skin(md: Dict, part: dict) -> Tuple[List[int], List[float]]:
    """Four bones and four weights a vertex of the part, strongest first."""
    weights = md["Vertices"][1].get("SkinWeights")
    table = weights.channels[0] if weights is not None and weights.channels else []
    indices: List[int] = []
    amounts: List[float] = []
    for vertex in part["sources"]:
        packed = table[vertex] if vertex < len(table) else []
        pulls = sorted(((v >> 16, (v & 0xFFFF) / 65535.0) for v in packed),
                       key=lambda p: -p[1])[:MAX_INFLUENCES]
        total = sum(w for _, w in pulls) or 1.0
        for slot in range(MAX_INFLUENCES):
            if slot < len(pulls):
                indices.append(pulls[slot][0])
                amounts.append(pulls[slot][1] / total)
            else:
                indices.append(0)
                amounts.append(0.0)
    return indices, amounts
