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

"""STL, PLY, OFF, 3MF and COLLADA, checked: `python3 meshtest.py [files…]`.

Each format's file is written here with the trap it is known for: a binary
STL whose colour is in the spare bytes, two solids in one text STL, a
big-endian PLY with colours per vertex, a PLY whose header runs past its
data, an OFF with a colour per face, a 3MF placing a component with a
transform and painting it from its base materials, and a Z-up COLLADA whose
node turns its geometry and whose numbers were written with decimal commas.
Files named on the command line are read and summed up.
"""

from __future__ import annotations

import io
import os
import struct
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import colladafile  # noqa: E402
import simplemesh  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        FAILED.append(name)


def simple(kind, data):
    found, pictures, z_up, unit, beside = simplemesh.read(kind, data)
    return simplemesh.meshes(found, pictures, z_up, beside)


def bounds(parts, axis):
    values = [v for p in parts for v in p["positions"][axis::3]]
    return min(values), max(values)


# -- STL ------------------------------------------------------------------------------

tri = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
colour = 0x8000 | (31 << 10)  # VisCAM: bit 15, then red in the top five bits
binary = b"no header".ljust(80, b"\0") + struct.pack("<I", 1) + \
    struct.pack("<12fH", 0, 0, 1, *[c for p in tri for c in p], colour)
parts, note = simple("stl", binary)
check("binary STL: one triangle", note["triangles"] == 1, note)
check("its colour from the spare bytes", parts[0]["color"] == "#FF0000", parts[0]["color"])
check("Z-up, stood up for the view: the triangle lies flat in X-Z",
      bounds(parts, 1) == (0.0, 0.0), bounds(parts, 1))

two = b"""solid a
facet normal 0 0 1
outer loop
vertex 0 0 0
vertex 1 0 0
vertex 0 1 0
endloop
endfacet
endsolid a
solid b
facet normal 0 0 1
outer loop
vertex 0 0 1
vertex 1 0 1
vertex 0 1 1
endloop
endfacet
endsolid b
"""
parts, note = simple("stl", two)
check("two solids in one text STL, two meshes", len(parts) == 2 and note["triangles"] == 2,
      [p["name"] for p in parts])

# -- PLY ------------------------------------------------------------------------------

header = (b"ply\nformat binary_big_endian 1.0\nelement vertex 3\n"
          b"property float x\nproperty float y\nproperty float z\n"
          b"property uchar red\nproperty uchar green\nproperty uchar blue\n"
          b"element face 1\nproperty list uchar int vertex_indices\nend_header\n")
body = b"".join(struct.pack(">3f3B", *p, 0, 0, 255) for p in tri) + struct.pack(">B3i", 3, 0, 1, 2)
parts, note = simple("ply", header + body)
check("big-endian binary PLY, colours per vertex",
      note["triangles"] == 1 and parts[0]["color"] == "#0000FF", (note, parts and parts[0]["color"]))
try:
    simple("ply", header + body[:20])
    check("a PLY cut short is refused, not a crash", False, "read")
except simplemesh.MeshFileError:
    check("a PLY cut short is refused, not a crash", True)

# -- OFF ------------------------------------------------------------------------------

off = b"""OFF
# a square, green
4 1 0
0 0 0
1 0 0
1 1 0
0 1 0
4 0 1 2 3 0.0 1.0 0.0
"""
parts, note = simple("off", off)
check("OFF: a square is two triangles, its face colour kept",
      note["triangles"] == 2 and parts[0]["color"] == "#00FF00", (note, parts[0]["color"]))

# -- 3MF ------------------------------------------------------------------------------

model = b"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <resources>
  <basematerials id="5"><base name="red" displaycolor="#FF0000FF"/></basematerials>
  <object id="1" type="model" pid="5" pindex="0">
   <mesh>
    <vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices>
    <triangles><triangle v1="0" v2="1" v3="2"/></triangles>
   </mesh>
  </object>
  <object id="2" type="model">
   <components><component objectid="1" transform="1 0 0 0 1 0 0 0 1 10 0 0"/></components>
  </object>
 </resources>
 <build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 5"/></build>
</model>
"""
packed = io.BytesIO()
with zipfile.ZipFile(packed, "w") as z:
    z.writestr("3D/3dmodel.model", model)
parts, note = simple("3mf", packed.getvalue())
low, high = bounds(parts, 0)
check("3MF: a component placed by its transform", low == 10.0 and high == 11.0, (low, high))
check("and the build item's transform after it, Z-up stood up",
      bounds(parts, 1) == (5.0, 5.0), bounds(parts, 1))
check("painted from its base materials", parts[0]["color"] == "#FF0000", parts[0]["color"])

# -- COLLADA --------------------------------------------------------------------------

dae = b"""<?xml version="1.0"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
 <asset><up_axis>Z_UP</up_axis><unit meter="0,01"/></asset>
 <library_effects><effect id="fx"><profile_COMMON><technique sid="t"><lambert>
  <diffuse><color>0,0 0,5 1,0 1</color></diffuse></lambert></technique></profile_COMMON></effect></library_effects>
 <library_materials><material id="blue"><instance_effect url="#fx"/></material></library_materials>
 <library_geometries><geometry id="g"><mesh>
  <source id="p"><float_array id="pa" count="12">0 0 0 1 0 0 1 1 0 0 1 0</float_array>
   <technique_common><accessor source="#pa" count="4" stride="3"/></technique_common></source>
  <vertices id="v"><input semantic="POSITION" source="#p"/></vertices>
  <polylist material="m" count="1"><input semantic="VERTEX" source="#v" offset="0"/>
   <vcount>4</vcount><p>0 1 2 3</p></polylist>
 </mesh></geometry></library_geometries>
 <library_visual_scenes><visual_scene id="s"><node id="n">
  <translate>0 0 7</translate>
  <instance_geometry url="#g"><bind_material><technique_common>
   <instance_material symbol="m" target="#blue"/></technique_common></bind_material></instance_geometry>
 </node></visual_scene></library_visual_scenes>
 <scene><instance_visual_scene url="#s"/></scene>
</COLLADA>
"""
scene = colladafile.read(dae)
parts, note = colladafile.meshes(scene)
check("COLLADA: a polylist quad is two triangles", note["triangles"] == 2, note)
check("its node's translate in Z, stood up to Y", bounds(parts, 1) == (7.0, 7.0), bounds(parts, 1))
check("a colour written with decimal commas", parts[0]["color"] == "#0080FF", parts[0]["color"])
packed = io.BytesIO()
with zipfile.ZipFile(packed, "w") as z:
    z.writestr("manifest.xml", "<dae_root>./inner/scene.dae</dae_root>")
    z.writestr("inner/scene.dae", dae)
parts, note = colladafile.meshes(colladafile.read(packed.getvalue()))
check("a .zae is the same document in a zip", note["triangles"] == 2, note)

# -- real files -----------------------------------------------------------------------

for path in sys.argv[1:]:
    started = time.time()
    kind = path.lower().rsplit(".", 1)[-1]
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        if kind in ("dae", "zae"):
            parts, note = colladafile.meshes(colladafile.read(data))
        else:
            parts, note = simple(kind, data)
        print("%s: %d mesh(es), %d triangles%s, %.2fs" % (
            os.path.basename(path), len(parts), note["triangles"],
            (", not drawn: %s" % note["other"]) if note.get("other") else "",
            time.time() - started))
    except (simplemesh.MeshFileError, colladafile.ColladaError) as failure:
        print("%s: refused — %s" % (os.path.basename(path), failure))

print("\n%d failure(s)" % len(FAILED) if FAILED else "\nall good")
sys.exit(1 if FAILED else 0)
