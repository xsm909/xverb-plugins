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

"""Maya Binary — `.mb` — read into the same :class:`mayafile.Scene` a `.ma`
becomes, so everything after the reading is shared.

**The container is IFF**, as Maya has written it since the beginning: a
chunk is a four-letter tag, four bytes nobody reads, an eight-byte big-endian
length (`FOR8`, `LIS8`, the 64-bit form Maya 2014 and later write; `FOR4`
and a four-byte length before that), and its data, padded to eight. A group
— `FOR8` — says its type in four letters and holds chunks; its length is
exact and is not padded.

**What is in it is the script of a `.ma`, already parsed.** Each node is a
group whose type is a four-letter code — `XFRM` a transform, `DMSH` a mesh,
`JOIN` a joint, `PCUB` a polyCube — holding `CREA` (the name and the parent)
and one chunk per `setAttr`, tagged by the kind of value: `DBLE`, `DBL3`,
`FLT2`, `STR `, `MATR`… each with the attribute's name, a flag byte and the
value, big-endian. `SLCT` picks an existing node the way `select` does, and
`CONN` holds the connections, one `CWFL` each.

**A mesh's shape is one `MESH` chunk**: the points; the edges as pairs, the
first index carrying the hard-edge bit; the faces as edge indices, the top
bit meaning "walked backwards" and the next two the face's last edge; then
each picture-coordinate set by name, its coordinates and one index per face
corner.

The codes below are the ones Maya writes. Those met in the files this was
built against are marked; the rest are Maya's type ids as documented, and a
code not known here is kept as the node's type, which only matters when it
stands between a mesh and the shape it came from.
"""

from __future__ import annotations

import struct
from typing import List, Optional, Tuple

from mayafile import MayaError, Node, Scene

_GROUPS8 = {b"FOR8", b"LIS8", b"CAT8", b"PRO8"}
_GROUPS4 = {b"FOR4", b"LIS4", b"CAT4", b"PRO4"}

_TYPES = {
    # Met in the sample files.
    "XFRM": "transform", "DMSH": "mesh", "JOIN": "joint", "PCUB": "polyCube",
    "DCAM": "camera", "NCRV": "nurbsCurve", "NCRC": "makeNurbCircle",
    "FSCL": "skinCluster", "FMPT": "tweak", "GRPP": "groupParts",
    "GPID": "groupId", "OBST": "objectSet", "FPOS": "dagPose",
    "SCRP": "script",
    # Maya's type ids, not met yet.
    "PSPH": "polySphere", "PPLN": "polyPlane", "PCYL": "polyCylinder",
    "PCON": "polyCone", "PTOR": "polyTorus", "PTWK": "polyTweak",
    "PTUV": "polyTweakUV", "FBSH": "blendShape", "SHDG": "shadingEngine",
    "RLAM": "lambert", "RBLN": "blinn", "RPHO": "phong", "FILE": "file",
    "LOCT": "locator", "NSRF": "nurbsSurface", "ANCA": "animCurveTA",
    "ANCL": "animCurveTL", "ANCU": "animCurveTU", "ANCT": "animCurveTT",
    "DLIT": "directionalLight", "PLIT": "pointLight", "SLIT": "spotLight",
    "ALIT": "areaLight", "AMBL": "ambientLight",
}


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        wide = data[:4] in (b"FOR8",)
        self.head = 16 if wide else 8
        self.groups = _GROUPS8 if wide else _GROUPS4
        self.align = 8 if wide else 4

    def chunks(self, at: int, end: int):
        data = self.data
        while at + self.head <= end:
            tag = data[at:at + 4]
            if self.head == 16:
                size = struct.unpack(">Q", data[at + 8:at + 16])[0]
            else:
                size = struct.unpack(">I", data[at + 4:at + 8])[0]
            body = at + self.head
            if body + size > len(data):
                raise MayaError("The file ends inside a chunk: it is cut short.")
            if tag in self.groups:
                yield tag, data[body:body + 4], body + 4, body + size
                at = body + size
            else:
                yield tag, None, body, body + size
                at = body + ((size + self.align - 1) & ~(self.align - 1))


def _cstrings(blob: bytes) -> List[bytes]:
    return blob.split(b"\0")


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _crea(blob: bytes) -> Tuple[str, Optional[str]]:
    """A node's name and, for a node in the hierarchy, its parent's.

    After the flag byte come the name and — only when the node has one —
    the parent, each ending in a zero; the node's id follows, and it is not
    text, which is how the absence of a parent is told."""
    parts = blob[1:].split(b"\0")
    name = _text(parts[0])
    parent = None
    if len(parts) > 2 and parts[1] and all(32 <= c < 127 or c >= 0xC0 for c in parts[1]):
        parent = _text(parts[1])
    return name, parent


def _attribute(tag: bytes, blob: bytes) -> Optional[Tuple[str, list, Optional[str]]]:
    """(attribute, values as the .ma would have written them, value type)."""
    stop = blob.find(b"\0")
    if stop < 0:
        return None
    name = _text(blob[:stop])
    value = blob[stop + 2:]          # past the name's zero and the flag byte
    kind = tag.decode("ascii", "replace")
    if kind in ("DBLE", "DBL2", "DBL3", "MATR", "DBL#", "DBL4"):
        count = len(value) // 8
        return name, [repr(v) for v in struct.unpack(">%dd" % count, value[:count * 8])], None
    if kind in ("FLT2", "FLT3", "FLT#", "FLT4", "FLT "):
        count = len(value) // 4
        return name, [repr(v) for v in struct.unpack(">%df" % count, value[:count * 4])], None
    if kind in ("I32#", "I32 ", "INT ", "INT2", "INT3", "LNG2", "LNG3"):
        count = len(value) // 4
        return name, [str(v) for v in struct.unpack(">%di" % count, value[:count * 4])], None
    if kind == "STR ":
        return name, [_text(value.split(b"\0")[0])], "string"
    return None


def _mesh(node: Node, value: bytes) -> None:
    """A `MESH` chunk into the `.vt`, `.ed`, `.fc` and UV attributes the
    `.ma` reader fills — so the rest of the way is the same."""
    at = 0

    def u32():
        nonlocal at
        (v,) = struct.unpack(">I", value[at:at + 4])
        at += 4
        return v

    count = u32()
    points = struct.unpack(">%df" % count, value[at:at + count * 4])
    at += count * 4
    node.attrs["vt"] = [(0, [repr(p) for p in points])]

    count = u32()
    raw = struct.unpack(">%dI" % count, value[at:at + count * 4])
    at += count * 4
    edges = []
    for k in range(0, len(raw) - 1, 2):
        a, b = raw[k], raw[k + 1]
        # The top bit of the first index is the hard edge.
        edges += [str(a & 0x7FFFFFFF), str(b & 0x7FFFFFFF), "0" if a & 0x80000000 else "1"]
    node.attrs["ed"] = [(0, edges)]

    count = u32()
    raw = struct.unpack(">%dI" % count, value[at:at + count * 4])
    at += count * 4
    faces: List[List[int]] = []
    ring: List[int] = []
    for word in raw:
        index = word & 0x1FFFFFFF
        ring.append(-index - 1 if word & 0x80000000 else index)
        if word & 0x60000000:
            faces.append(ring)
            ring = []
    if ring:
        faces.append(ring)

    corner_uvs: Optional[List[int]] = None
    try:
        holes = u32()
        at += holes * 4
        sets = u32()
        for set_index in range(sets):
            u32()
            end = value.index(b"\0", at)
            at = end + 1
            count = u32()
            uv = struct.unpack(">%df" % count, value[at:at + count * 4])
            at += count * 4
            count = u32()
            ids = list(struct.unpack(">%dI" % count, value[at:at + count * 4]))
            at += count * 4
            if set_index == 0:
                node.attrs["uvst[0].uvsp"] = [(0, [repr(v) for v in uv])]
                corner_uvs = ids
    except (struct.error, ValueError):
        corner_uvs = None

    words: List[str] = []
    corner = 0
    for ring in faces:
        words += ["f", str(len(ring))] + [str(e) for e in ring]
        if corner_uvs is not None and corner + len(ring) <= len(corner_uvs):
            words += ["mu", "0", str(len(ring))] + [str(i) for i in corner_uvs[corner:corner + len(ring)]]
        corner += len(ring)
    node.attrs["fc"] = [(0, words)]


def parse(data: bytes) -> Scene:
    if data[:4] not in (b"FOR8", b"FOR4"):
        raise MayaError("This is not a Maya binary scene.")
    reader = _Reader(data)
    scene = Scene()
    scene.binary = True

    def read_group(kind: bytes, start: int, end: int) -> None:
        code = kind.decode("ascii", "replace")
        node: Optional[Node] = None
        for tag, inner, body, stop in reader.chunks(start, end):
            if inner is not None:
                read_group(inner, body, stop)
                continue
            blob = data[body:stop]
            if code == "HEAD":
                _head(scene, tag, blob)
            elif code == "CONN":
                if tag == b"CWFL":
                    parts = [p for p in _cstrings(blob[1:]) if p]
                    if len(parts) >= 2:
                        scene.connect(_text(parts[0]).lstrip(":"), _text(parts[1]).lstrip(":"))
            elif tag == b"CREA":
                name, parent = _crea(blob)
                node = scene.add(_TYPES.get(code, code), name, parent)
            elif tag == b"SLCT" and code == "SLCT":
                name = _text(blob.split(b"\0")[0]).lstrip(":")
                node = scene.node(name) or scene.add("unknown", name, None)
            elif node is not None and tag == b"MESH":
                try:
                    _mesh(node, blob[blob.index(b"\0") + 2:])
                except (struct.error, ValueError):
                    pass
            elif node is not None:
                found = _attribute(tag, blob)
                if found is not None:
                    attr, values, kind_name = found
                    start_index = None
                    if "[" in attr and attr.endswith("]") and attr.count("[") == attr.count("]"):
                        head, _, index = attr.rpartition("[")
                        first = index[:-1].split(":")[0]
                        if first.lstrip("-").isdigit():
                            attr, start_index = head, int(first)
                    node.attrs.setdefault(attr, []).append((start_index, values))

    for tag, inner, body, stop in reader.chunks(0, len(data)):
        if inner is not None:
            for tag2, inner2, body2, stop2 in reader.chunks(body, stop):
                if inner2 is not None:
                    read_group(inner2, body2, stop2)
    scene.settle()
    return scene


def _head(scene: Scene, tag: bytes, blob: bytes) -> None:
    text = _text(blob.split(b"\0")[0])
    if tag == b"VERS":
        scene.version = text
    elif tag == b"LUNI":
        scene.unit = text
    elif tag == b"TUNI":
        scene.rate = text
    elif tag == b"FINF":
        parts = [_text(p) for p in _cstrings(blob)]
        if len(parts) >= 2 and parts[0] == "product":
            scene.creator = parts[1]
    elif tag == b"INCL" and text and text != "undef":
        scene.references.append(text)
