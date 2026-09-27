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

"""Houdini geometry, checked without Houdini: `python3 houdinitest.py [files…]`.

The documents are written here in the shapes Houdini writes: the JSON form
with a polygon run and paged attributes, the same document in binary JSON,
the classic text format, and a tetrahedron. Real files named on the command
line — `.bgeo.sc` among them, which is where the decompression is checked —
are read and summed up.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import houdinigeo  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        FAILED.append(name)


# A unit quad and a triangle beside it, as a polygon run. The points' P is
# paged; Cd has one constant page, which is written once.
DOC = [
    "fileversion", "20.5.278", "pointcount", 5, "vertexcount", 7, "primitivecount", 2,
    "info", {"software": "Houdini 20.5.278"},
    # Clockwise seen from above, as Houdini winds a face that looks up.
    "topology", ["pointref", ["indices", [0, 3, 2, 1, 3, 4, 2]]],
    "attributes", ["pointattributes", [
        [["scope", "public", "type", "numeric", "name", "P"],
         ["size", 3, "storage", "fpreal32",
          "values", ["size", 3, "storage", "fpreal32", "pagesize", 1024,
                     "rawpagedata", [0, 0, 0, 0, 0, 1, 1, 0, 1, 1, 0, 0, 2, 0, 0]]]],
        [["scope", "public", "type", "numeric", "name", "Cd"],
         ["size", 3, "storage", "fpreal32",
          "values", ["size", 3, "storage", "fpreal32", "pagesize", 1024,
                     "constantpageflags", [[True]], "rawpagedata", [1.0, 0.0, 0.0]]]],
    ]],
    "primitives", [[["type", "p_r"], ["s_v", 0, "n_p", 2, "r_v", [4, 1, 3, 1]]]],
]

parts, note = houdinigeo.meshes(DOC)
check("a quad and a triangle: three triangles", note["triangles"] == 3, note)
check("a constant page is the whole page", parts[0]["color"] == "#FF0000", parts[0]["color"])
ys = [round(n, 6) for n in parts[0]["normals"][1::3]]
check("wound as Houdini winds, facing up after the turn round", all(y == 1.0 for y in ys), ys)


def to_binary(value) -> bytes:
    """Houdini's binary JSON of [value], the plain way: no tokens, no packing."""
    out = bytearray()

    def length(n):
        if n < 0xF1:
            out.append(n)
        else:
            out.append(0xF4)
            out.extend(struct.pack("<I", n))

    def put(v):
        if isinstance(v, bool):
            out.append(0x31 if v else 0x30)
        elif isinstance(v, int):
            out.append(0x13)
            out.extend(struct.pack("<i", v))
        elif isinstance(v, float):
            out.append(0x19)
            out.extend(struct.pack("<f", v))
        elif isinstance(v, str):
            out.append(0x27)
            raw = v.encode()
            length(len(raw))
            out.extend(raw)
        elif isinstance(v, list):
            out.append(0x5B)
            for x in v:
                put(x)
            out.append(0x5D)
        elif isinstance(v, dict):
            out.append(0x7B)
            for k, x in v.items():
                put(k)
                put(x)
            out.append(0x7D)

    out.extend(b"\x7fNSJb")
    put(value)
    return bytes(out)


binary = houdinigeo.read(to_binary(DOC))
check("the binary JSON reads back as the same document", binary == DOC)
text = houdinigeo.read(json.dumps(DOC).encode())
check("so does the text JSON", text == DOC)

CLASSIC = b"""PGEOMETRY V5
NPoints 4 NPrims 2
NPointGroups 0 NPrimGroups 0
NPointAttrib 0 NVertexAttrib 0 NPrimAttrib 1 NAttrib 0
0 0 0 1
0 0 1 1
1 0 1 1
1 0 0 1
PrimitiveAttrib
generator 1 index 1 papi
Poly 4 < 0 1 2 3 [0]
Poly 2 : 0 2 [0]
"""
parts, note = houdinigeo.meshes(houdinigeo.read(CLASSIC))
check("the classic format: a closed quad drawn, an open one counted",
      note["triangles"] == 2 and note["other"] == {"open polygons": 1}, note)

TET = [
    "pointcount", 4, "vertexcount", 4, "primitivecount", 1,
    "topology", ["pointref", ["indices", [0, 1, 2, 3]]],
    "attributes", ["pointattributes", [[["type", "numeric", "name", "P"],
                                       ["size", 3, "values", ["size", 3, "tuples",
                                        [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]]]]]],
    "primitives", [[["type", "t_r"], ["s_v", 0, "n_p", 1]]],
]
parts, note = houdinigeo.meshes(TET)
positions = parts[0]["positions"]
centre = [sum(positions[k::3]) / (len(positions) // 3) for k in range(3)]
indices = parts[0]["indices"]
outward = 0
for t in range(0, len(indices), 3):
    a, b, c = (positions[i * 3:i * 3 + 3] for i in indices[t:t + 3])
    u = [b[k] - a[k] for k in range(3)]
    v = [c[k] - a[k] for k in range(3)]
    n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
    face = [(a[k] + b[k] + c[k]) / 3 - centre[k] for k in range(3)]
    outward += sum(n[k] * face[k] for k in range(3)) > 0
check("a tetrahedron is its four faces, all facing out",
      note["triangles"] == 4 and outward == 4, (note["triangles"], outward))

for path in sys.argv[1:]:
    started = time.time()
    with open(path, "rb") as handle:
        doc = houdinigeo.read(handle.read())
    parts, note = houdinigeo.meshes(doc, max_triangles=10 ** 7)
    print("%s: %d triangles%s, %.2fs" % (
        os.path.basename(path), note["triangles"],
        (", not drawn: %s" % note["other"]) if note.get("other") else "", time.time() - started))

print("\n%d failure(s)" % len(FAILED) if FAILED else "\nall good")
sys.exit(1 if FAILED else 0)
