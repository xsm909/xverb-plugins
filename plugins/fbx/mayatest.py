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

"""Maya scenes, checked without Maya: `python3 mayatest.py [scenes…]`.

The scenes are written here, in the words Maya writes them in. The numbers the
primitives are held to are the ones Maya's own polySphere, polyCube and the
rest produce at their defaults. Real scenes named on the command line are read
and summed up.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mayafile  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        FAILED.append(name)


# A cube Maya wrote out whole: eight points, twelve hard edges, six faces.
CUBE = r'''//Maya ASCII 2020 scene
requires maya "2020";
currentUnit -l centimeter -a degree -t film;
fileInfo "product" "Maya 2020";
createNode transform -n "box";
	setAttr ".t" -type "double3" 10 0 0 ;
	setAttr ".r" -type "double3" 0 90 0 ;
createNode mesh -n "boxShape" -p "box";
	setAttr -s 8 ".vt[0:7]"  -0.5 -0.5 0.5 0.5 -0.5 0.5 -0.5 0.5 0.5 0.5 0.5 0.5
		 -0.5 0.5 -0.5 0.5 0.5 -0.5 -0.5 -0.5 -0.5 0.5 -0.5 -0.5;
	setAttr -s 12 ".ed[0:11]"  0 1 0 2 3 0 4 5 0 6 7 0 0 2 0 1 3 0 2 4 0 3 5 0
		 4 6 0 5 7 0 6 0 0 7 1 0;
	setAttr -s 6 -ch 24 ".fc[0:5]" -type "polyFaces"
		f 4 0 5 -2 -5
		f 4 1 7 -3 -7
		f 4 2 9 -4 -9
		f 4 3 11 -1 -11
		f 4 -12 -10 -8 -6
		f 4 10 4 6 8 ;
createNode transform -n "copy";
	setAttr ".t" -type "double3" 0 5 0 ;
createNode lambert -n "red";
	setAttr ".c" -type "float3" 1 0 0 ;
createNode shadingEngine -n "redSG";
createNode transform -n "hidden";
	setAttr ".v" no;
createNode mesh -n "hiddenShape" -p "hidden";
createNode polySphere -n "polySphere1";
createNode transform -n "ball";
createNode mesh -n "ballShape" -p "ball";
createNode polyCube -n "polyCube1";
connectAttr "polySphere1.out" "ballShape.i";
connectAttr "polyCube1.out" "hiddenShape.i";
connectAttr "red.oc" "redSG.ss";
connectAttr "|box|boxShape.iog" "redSG.dsm" -na;
parent -s -nc -r -add "|box|boxShape" "copy" ;
'''

scene = mayafile.parse(CUBE)
parts, note = mayafile.meshes(scene)
names = sorted(p["name"] for p in parts)
check("a stored mesh, its instance, and a sphere; the hidden one left out",
      names == ["ball", "box", "copy"], names)
box = [p for p in parts if p["name"] == "box"][0]
check("the cube is twelve triangles, on 24 corners — four per flat face",
      len(box["indices"]) == 36 and len(box["positions"]) == 72, (len(box["indices"]), len(box["positions"])))
xs = box["positions"][0::3]
zs = box["positions"][2::3]
check("placed by its transform: turned, then moved ten along x",
      abs(min(xs) - 9.5) < 1e-6 and abs(max(xs) - 10.5) < 1e-6
      and abs(min(zs) + 0.5) < 1e-6, (min(xs), max(xs), min(zs)))
copy = [p for p in parts if p["name"] == "copy"][0]
ys = copy["positions"][1::3]
check("the instance stands where its own parent puts it", abs(min(ys) - 4.5) < 1e-6, min(ys))
check("hard edges shade flat: a corner's normal is its face's",
      all(abs(abs(v) - 1.0) < 1e-6 or abs(v) < 1e-6 for v in box["normals"]))
check("the material's colour", box["color"] == "#FF0000", box["color"])
facts = mayafile.summarise(scene, parts, note)
check("what the file says about itself", facts["mayaVersion"] == "2020"
      and facts["unitScale"] == 1.0 and facts["frameRate"] == 24.0 and facts["creator"] == "Maya 2020",
      facts)

# The primitives at their defaults, counted as Maya counts them.
EXPECTED = {"polySphere": (400, 760), "polyCube": (6, 12), "polyCone": (21, 38),
            "polyCylinder": (22, 76), "polyTorus": (400, 800), "polyPlane": (100, 200)}
for kind, (faces, triangles) in EXPECTED.items():
    built = mayafile.primitive(mayafile.Node(kind, kind, None))
    check("%s: %d faces, %d triangles" % (kind, faces, triangles),
          (len(built.faces), built.triangles) == (faces, triangles),
          (len(built.faces), built.triangles))

# An axis other than Y turns the primitive.
node = mayafile.Node("polyCylinder", "c", None)
node.attrs["ax"] = [(None, ["1", "0", "0"])]
built = mayafile.primitive(node)
xs = [p[0] for p in built.points]
check("a cylinder on the x axis runs along x", abs(max(xs) - 1.0) < 1e-6 and abs(min(xs) + 1.0) < 1e-6,
      (min(xs), max(xs)))

# Modelling history is named, not guessed at.
HISTORY = CUBE + '''createNode polyExtrudeFace -n "polyExtrudeFace1";
createNode transform -n "made";
createNode mesh -n "madeShape" -p "made";
connectAttr "polyCube1.out" "polyExtrudeFace1.ip";
connectAttr "polyExtrudeFace1.out" "madeShape.i";
'''
parts, note = mayafile.meshes(mayafile.parse(HISTORY))
check("a mesh behind an extrusion is counted, not drawn",
      note["unbuilt"] == {"polyExtrudeFace": 1} and "made" not in [p["name"] for p in parts],
      note["unbuilt"])

for path in sys.argv[1:]:
    with open(path, "rb") as handle:
        found = mayafile.read(handle.read())
    parts, note = mayafile.meshes(found)
    print("%s: %d mesh(es), %d triangles%s" % (
        os.path.basename(path), len(parts), note["triangles"],
        (", not drawn: %s" % note["unbuilt"]) if note["unbuilt"] else ""))

print("\n%d failure(s)" % len(FAILED) if FAILED else "\nall good")
sys.exit(1 if FAILED else 0)
