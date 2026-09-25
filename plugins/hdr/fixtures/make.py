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

"""The fixtures, written by Blender rather than by this plugin.

A reader checked only against files its own author wrote checks that the two
agree with each other. These come from OpenEXR itself, through Blender, in
every compression Blender offers, and `source.json` is the pixels they were
made from — so the self-test compares against what went in, not against what
another reader says came out.

    Blender --background --factory-startup --python make.py
"""

import json
import math
import os

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
W, H = 45, 29


def value(x, y, c):
    """Linear values past 1, below 0 nowhere, a different ramp per channel."""
    if c == 0:
        return x / (W - 1) * 4.0
    if c == 1:
        return y / (H - 1)
    if c == 2:
        return 0.5 + 0.5 * math.sin(x * 0.7) * math.cos(y * 0.5)
    return 1.0 if (x + y) % 3 else 0.25


# EXR rows run top to bottom; Blender's pixel buffer runs bottom to top.
top_down = [[[value(x, y, c) for c in range(4)] for x in range(W)] for y in range(H)]
flat = []
for y in reversed(range(H)):
    for x in range(W):
        flat.extend(top_down[y][x])

image = bpy.data.images.new("fixture", W, H, alpha=True, float_buffer=True)
image.colorspace_settings.name = "Linear Rec.709"
image.pixels = flat

scene = bpy.context.scene
settings = scene.render.image_settings
settings.file_format = "OPEN_EXR"
settings.color_mode = "RGBA"

for depth in ("16", "32"):
    for codec in ("NONE", "RLE", "ZIPS", "ZIP", "PIZ", "PXR24", "B44", "B44A", "DWAA", "DWAB"):
        if depth == "32" and codec in ("B44", "B44A"):
            continue  # Blender refuses: B44 is for halves only
        settings.color_depth = depth
        settings.exr_codec = codec
        image.save_render(os.path.join(HERE, "rgba_%s_%s.exr" % (depth, codec.lower())), scene=scene)

settings.file_format = "HDR"
settings.color_mode = "RGB"
image.save_render(os.path.join(HERE, "rgb.hdr"), scene=scene)

# A multilayer render: the layout Blender writes for every pass it has.
scene.render.engine = "BLENDER_WORKBENCH"
scene.render.resolution_x, scene.render.resolution_y = 40, 24
scene.render.resolution_percentage = 100
scene.view_layers[0].use_pass_z = True
settings.media_type = "MULTI_LAYER_IMAGE"
settings.file_format = "OPEN_EXR_MULTILAYER"
settings.color_depth = "16"
settings.exr_codec = "ZIP"
# Blender 5 writes a part per pass; the one-part layout with every pass's
# channels side by side is what it wrote before, and what files in the wild
# still are. One of each.
for interleave, name in ((False, "multipart.exr"), (True, "multilayer.exr")):
    settings.use_exr_interleave = interleave
    scene.render.filepath = os.path.join(HERE, name)
    bpy.ops.render.render(write_still=True)

with open(os.path.join(HERE, "source.json"), "w") as out:
    json.dump({"width": W, "height": H, "pixels": top_down}, out)
