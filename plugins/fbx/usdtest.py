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

"""USD, checked without USD: `python3 usdtest.py [files…]`.

The two compressions of the binary layer against hand-made bytes, then a
small stage written here in text — a reference with its material carried
across, a variant, a sublayer, a pivot undone by `!invert!`, a subset with its
own material, a left-handed mesh and a cube made from its size. Files named
on the command line are composed and summed up.
"""

from __future__ import annotations

import os
import struct
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import usdcrate  # noqa: E402
import usdfile  # noqa: E402
import usdtext  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        FAILED.append(name)


# -- the binary layer's compressions ---------------------------------------------------

check("LZ4: literals alone", usdcrate.lz4_block(b"\x50hello") == b"hello")
check("LZ4: a match that overlaps itself",
      usdcrate.lz4_block(b"\x35abc\x03\x00") == b"abcabcabcabc",
      usdcrate.lz4_block(b"\x35abc\x03\x00"))
check("the chunked wrapper, one block", usdcrate.fast_decompress(b"\x00\x50hello") == b"hello")
two = b"\x02" + struct.pack("<i", 4) + b"\x30abc" + struct.pack("<i", 4) + b"\x30def"
check("the chunked wrapper, two chunks", usdcrate.fast_decompress(two) == b"abcdef")

# Deltas 1, 1, 300, -2: common 1 twice, then a 16-bit and an 8-bit one.
coded = struct.pack("<i", 1) + bytes([0 | 0 << 2 | 2 << 4 | 1 << 6]) + struct.pack("<hb", 300, -2)
check("integer coding: common, 16-bit and 8-bit deltas",
      usdcrate.decode_ints(coded, 4) == [1, 2, 302, 300], usdcrate.decode_ints(coded, 4))

# -- a stage made of text layers -------------------------------------------------------

FILES = {
    "ball.usda": b'''#usda 1.0
(
    defaultPrim = "Ball"
)
def Xform "Ball"
{
    def Mesh "Shape" (
        prepend apiSchemas = ["MaterialBindingAPI"]
    )
    {
        int[] faceVertexCounts = [4]
        int[] faceVertexIndices = [0, 1, 2, 3]
        point3f[] points = [(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)]
        rel material:binding = </Ball/Looks/Red>
    }
    def Scope "Looks"
    {
        def Material "Red"
        {
            token outputs:surface.connect = </Ball/Looks/Red/Surface.outputs:surface>
            def Shader "Surface"
            {
                uniform token info:id = "UsdPreviewSurface"
                color3f inputs:diffuseColor = (1, 0, 0)
                token outputs:surface
            }
        }
    }
}
''',
    "extra.usda": b'''#usda 1.0
over "World"
{
    def Cube "Box"
    {
        double size = 4
        double3 xformOp:translate = (10, 0, 0)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }
}
''',
    "stage.usda": b'''#usda 1.0
(
    subLayers = [@./extra.usda@]
    upAxis = "Z"
    metersPerUnit = 1
)
def Xform "World"
{
    def Xform "Toy" (
        prepend references = @./ball.usda@
    )
    {
        double3 xformOp:translate = (0, 5, 0)
        float3 xformOp:translate:pivot = (1, 1, 1)
        uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:translate:pivot", "!invert!xformOp:translate:pivot"]
    }
    def Xform "Choice" (
        variants = {
            string look = "second"
        }
        prepend variantSets = "look"
    )
    {
        variantSet "look" = {
            "first" {
                def Mesh "Only" { int[] faceVertexCounts = [3] int[] faceVertexIndices = [0, 1, 2] point3f[] points = [(0,0,0), (1,0,0), (0,1,0)] }
            }
            "second" {
                def Mesh "Only" { int[] faceVertexCounts = [3] int[] faceVertexIndices = [0, 1, 2] point3f[] points = [(0,0,0), (2,0,0), (0,2,0)] }
            }
        }
    }
    def Mesh "Mirror"
    {
        uniform token orientation = "leftHanded"
        int[] faceVertexCounts = [3]
        int[] faceVertexIndices = [0, 1, 2]
        point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
    }
    def Mesh "Split"
    {
        int[] faceVertexCounts = [3, 3]
        int[] faceVertexIndices = [0, 1, 2, 0, 2, 3]
        point3f[] points = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
        color3f[] primvars:displayColor = [(0, 0, 1)]
        def GeomSubset "Green" (
            prepend apiSchemas = ["MaterialBindingAPI"]
        )
        {
            uniform token elementType = "face"
            uniform token familyName = "materialBind"
            int[] indices = [1]
            rel material:binding = </World/Looks/Green>
        }
    }
    def Scope "Looks"
    {
        def Material "Green"
        {
            token outputs:surface.connect = <Surface.outputs:surface>
            def Shader "Surface"
            {
                uniform token info:id = "UsdPreviewSurface"
                color3f inputs:diffuseColor = (0, 1, 0)
            }
        }
    }
    def Mesh "Hidden"
    {
        token visibility = "invisible"
        int[] faceVertexCounts = [3]
        int[] faceVertexIndices = [0, 1, 2]
        point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
    }
}
''',
}


def read(url: str):
    name = urllib.parse.unquote(url).rsplit("/", 1)[-1]
    return FILES.get(name)


stage = usdfile.read("file:///made/stage.usda", read)
parts, note = usdfile.meshes(stage)
by_name = {}
for part in parts:
    by_name.setdefault(part["name"], []).append(part)

check("the sublayer's box is drawn", "Box" in by_name, sorted(by_name))
box = by_name.get("Box", [{}])[0]
xs = box.get("positions", [0])[0::3]
check("the box is its size, where its translate puts it",
      abs(min(xs) - 8) < 1e-6 and abs(max(xs) - 12) < 1e-6, (min(xs), max(xs)))

shape = by_name.get("Shape", [{}])[0]
ys = set(round(y, 6) for y in shape.get("positions", [])[1::3])
check("the referenced mesh is drawn under the prim that references it", ys == {5.0}, ys)
check("its material, bound inside its own file, is carried across", shape.get("color") == "#FF0000",
      shape.get("color"))

only = by_name.get("Only", [{}])[0]
check("the selected variant, not the first", max(only.get("positions", [0])) == 2.0,
      only.get("positions"))

mirror = by_name.get("Mirror", [{}])[0]
check("a left-handed triangle is turned to face the same way",
      mirror.get("normals", [0, 0, 0])[2] < 0, mirror.get("normals"))

split = by_name.get("Split", [])
colours = sorted(p["color"] for p in split)
check("a subset wears its own material, the rest the display colour",
      len(split) == 2 and "#00FF00" in colours and "#0000FF" in colours, colours)
check("an invisible mesh is not drawn", "Hidden" not in by_name)
check("the stage says Z is up and a unit is a metre",
      stage.up_axis == "Z" and abs(usdfile.summarise(stage, parts, note, 0)["unitScale"] - 100) < 1e-9)

# -- the text reader's corners ---------------------------------------------------------

layer = usdtext.parse(b'''#usda 1.0
def "A" (
    prepend references = @x.usd@</B> (offset = 2)
    doc = """two
lines"""
)
{
    custom double3 xformOp:translate.timeSamples = {
        1: (1, 2, 3),
        2: (4, 5, 6),
    }
    rel target = [<../C>, </D>]
}
''')
refs = layer["/A"]["fields"]["references"].applied()
check("a reference with a prim and a layer offset", refs and refs[0].asset == "x.usd"
      and refs[0].prim == "/B", refs)
samples = layer["/A.xformOp:translate"]["fields"]["timeSamples"]
check("time samples, and the earliest is the one drawn",
      usdcrate.sample(samples) == (1.0, 2.0, 3.0), samples)
check("a relative target made absolute",
      layer["/A.target"]["fields"]["targetPaths"].applied() == ["/C", "/D"],
      layer["/A.target"]["fields"]["targetPaths"].applied())

# -- real files -----------------------------------------------------------------------

def from_disk(url: str):
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    return open(path, "rb").read() if os.path.isfile(path) else None


for path in sys.argv[1:]:
    started = time.time()
    try:
        stage = usdfile.read("file://" + urllib.parse.quote(os.path.abspath(path)), from_disk)
        parts, note = usdfile.meshes(stage)
        facts = usdfile.summarise(stage, parts, note, 0)
        print("%s: %d mesh(es), %d triangles, %d layer(s)%s%s, %.2fs" % (
            os.path.basename(path), len(parts), note["triangles"], facts["usdLayers"],
            ", %d not found" % len(facts["usdLayersMissing"]) if facts["usdLayersMissing"] else "",
            ", %d skipped past the cap" % note["droppedMeshes"] if note["droppedMeshes"] else "",
            time.time() - started))
    except Exception as failure:  # noqa: BLE001
        print("%s: %s" % (os.path.basename(path), failure))

print("\n%d failure(s)" % len(FAILED) if FAILED else "\nall good")
sys.exit(1 if FAILED else 0)
