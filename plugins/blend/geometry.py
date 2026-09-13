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

"""Placing and triangulating what the file holds, into world-space triangles.

**Two ways of storing a mesh, one reader.** Blender 4.0 took positions and face
corners out of named struct fields and put them into generic attribute layers,
and both shapes are live in one folder of the user's files. Nothing here
branches on the version: it asks what the file has and uses that.

|              | up to 3.6                 | 4.0 and later                |
| ------------ | ------------------------- | ---------------------------- |
| positions    | `MVert.co` via `mvert`    | vertex layer `position`      |
| face corners | `MLoop.v` via `mloop`     | corner layer `.corner_vert`  |
| face extents | `MPoly.loopstart/totloop` | `*_offset_indices`, N+1 of them |

The same is true of where an object stands. Up to 3.6 the composed world matrix
is in the file as `obmat`; from 4.0 it is worked out when the file opens and is
not saved at all, so it has to be built here out of the parts that are —
position, rotation in whichever of seven modes the object uses, scale, the
delta transforms, and the parent chain.
"""

from __future__ import annotations

import math
import struct
import sys
from array import array
from typing import Dict, List, Optional, Tuple

import catalog
import shading
from blendfile import Block, BlendFile

#: A guard, not a target. The measured corpus tops out at 114 574 triangles and
#: the renderer is smooth to about 150 000, but a scene file could hold far
#: more, and a preview that takes a minute is not a preview.
MAX_TRIANGLES = 400000

#: `Object.type` for a mesh, and for a rig.
OB_MESH = 1
OB_ARMATURE = 25

#: How many bone ends one rig may send. The host numbers a bone's parent in
#: sixteen bits, and a rig past this is a crowd scene rather than a character.
MOST_ENDS = 8000

#: `Object.rotmode`. Zero is a quaternion, negative is axis-angle, and the six
#: positive values are the orders the three euler angles are applied in.
ROTATION_ORDERS = {
    1: "XYZ", 2: "XZY", 3: "YXZ", 4: "YZX", 5: "ZXY", 6: "ZYX",
}

#: `MPoly.flag`. The bit that says the face is drawn smooth.
ME_SMOOTH = 1

IDENTITY = [1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0]

_NATIVE_LITTLE = sys.byteorder == "little"


# -- matrices --------------------------------------------------------------
#
# Blender stores a matrix as `float m[4][4]` with the translation in `m[3]`, so
# flattened it is column-major: element `i * 4 + j` is `m[i][j]`, and the last
# four are the translation. Everything here keeps that layout, because it is
# the layout the file already uses.

def multiply(a: List[float], b: List[float]) -> List[float]:
    """`a` then `b`, in Blender's own layout."""
    out = [0.0] * 16
    for i in range(4):
        for j in range(4):
            out[i * 4 + j] = (
                a[i * 4 + 0] * b[0 * 4 + j] +
                a[i * 4 + 1] * b[1 * 4 + j] +
                a[i * 4 + 2] * b[2 * 4 + j] +
                a[i * 4 + 3] * b[3 * 4 + j])
    return out


def euler_matrix(x: float, y: float, z: float, order: str) -> List[float]:
    """Three angles in radians, applied in the order the object asks for."""
    axes = {}
    for name, angle in (("X", x), ("Y", y), ("Z", z)):
        c, s = math.cos(angle), math.sin(angle)
        if name == "X":
            axes[name] = [1, 0, 0, 0, 0, c, s, 0, 0, -s, c, 0, 0, 0, 0, 1]
        elif name == "Y":
            axes[name] = [c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0, 0, 0, 0, 1]
        else:
            axes[name] = [c, s, 0, 0, -s, c, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    # Blender applies an "XYZ" euler as X first, so composing left to right in
    # that order with this multiply gives the matrix it would have built.
    out = [float(v) for v in axes[order[0]]]
    for name in order[1:]:
        out = multiply(out, [float(v) for v in axes[name]])
    return out


def quaternion_matrix(w: float, x: float, y: float, z: float) -> List[float]:
    length = math.sqrt(w * w + x * x + y * y + z * z)
    if length < 1e-12:
        return list(IDENTITY)
    w, x, y, z = w / length, x / length, y / length, z / length
    return [
        1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w), 0.0,
        2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w), 0.0,
        2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y), 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def axis_angle_matrix(axis: List[float], angle: float) -> List[float]:
    length = math.sqrt(sum(v * v for v in axis)) if axis else 0.0
    if length < 1e-12:
        return list(IDENTITY)
    x, y, z = (v / length for v in axis)
    return quaternion_matrix(math.cos(angle / 2.0),
                             x * math.sin(angle / 2.0),
                             y * math.sin(angle / 2.0),
                             z * math.sin(angle / 2.0))


def scale_matrix(x: float, y: float, z: float) -> List[float]:
    return [x, 0.0, 0.0, 0.0,
            0.0, y, 0.0, 0.0,
            0.0, 0.0, z, 0.0,
            0.0, 0.0, 0.0, 1.0]


#: Blender stands the world up on +Z; the host draws Y up, whatever the file
#: said. Height becomes height and nothing is mirrored: a helmet measured at
#: 160 up the Z axis arrives 160 up the Y axis.
def to_y_up(x: float, y: float, z: float) -> Tuple[float, float, float]:
    return x, z, -y


# -- where an object stands -------------------------------------------------

def local_matrix(f: BlendFile, block: Block) -> List[float]:
    """An object's own transform, built the way Blender builds it.

    Position, rotation and scale each have a *delta* beside them — a second
    set that animation and rigging use so the first can stay as the user typed
    it. Blender adds the deltas in, and a reader that ignores them puts every
    delta-posed object at the wrong place.
    """
    loc = f.numbers(block, "loc", 3) or [0.0, 0.0, 0.0]
    dloc = f.numbers(block, "dloc", 3) or [0.0, 0.0, 0.0]
    size = f.numbers(block, "size", 3) or [1.0, 1.0, 1.0]
    dscale = f.numbers(block, "dscale", 3) or [1.0, 1.0, 1.0]

    mode = f.value(block, "rotmode", default=1)
    if mode == 0:
        quat = f.numbers(block, "quat", 4) or [1.0, 0.0, 0.0, 0.0]
        dquat = f.numbers(block, "dquat", 4) or [1.0, 0.0, 0.0, 0.0]
        turn = multiply(quaternion_matrix(*dquat), quaternion_matrix(*quat))
    elif mode is not None and mode < 0:
        axis = f.numbers(block, "rotAxis", 3) or [0.0, 1.0, 0.0]
        angle = f.value(block, "rotAngle", default=0.0) or 0.0
        turn = axis_angle_matrix(axis, angle)
    else:
        order = ROTATION_ORDERS.get(mode or 1, "XYZ")
        rot = f.numbers(block, "rot", 3) or [0.0, 0.0, 0.0]
        drot = f.numbers(block, "drot", 3) or [0.0, 0.0, 0.0]
        turn = multiply(euler_matrix(*drot, order=order),
                        euler_matrix(*rot, order=order))

    out = multiply(scale_matrix(size[0] * dscale[0],
                                size[1] * dscale[1],
                                size[2] * dscale[2]), turn)
    out[12] = loc[0] + dloc[0]
    out[13] = loc[1] + dloc[1]
    out[14] = loc[2] + dloc[2]
    return out


def world_matrix(f: BlendFile, block: Block,
                 cache: Dict[int, List[float]]) -> List[float]:
    """Where the object really is, composed up the parent chain.

    Up to 3.6 the file simply carries the answer as `obmat` and that is used
    outright — it is what Blender itself last computed, and it already accounts
    for constraints and every parenting mode. From 4.0 it is not saved, so the
    chain is walked: each object's own transform, through the inverse its
    parent handed it, into the parent's world.

    Only object parenting is followed. A mesh parented to a *bone* or to three
    *vertices* of another mesh lands at its parent's origin instead — Blender
    resolves those against a posed armature that is not in the file either.
    """
    found = cache.get(block.address)
    if found is not None:
        return found

    saved = f.numbers(block, "obmat", 16)
    if len(saved) == 16:
        cache[block.address] = saved
        return saved

    out = local_matrix(f, block)
    parent = f.at_address(f.pointer(block, "parent", default=0) or 0)
    if parent is not None and parent.code == b"OB\0\0":
        # Guard the chain before walking it: a cycle is not a thing Blender
        # writes, but this reader is pointed at files it did not write.
        cache[block.address] = out
        inverse = f.numbers(block, "parentinv", 16)
        if len(inverse) == 16:
            out = multiply(out, inverse)
        out = multiply(out, world_matrix(f, parent, cache))
    cache[block.address] = out
    return out


# -- reading an array out of a block ----------------------------------------

def _typed(f: BlendFile, raw: bytes, code: str) -> array:
    """A whole block read as one kind of number, byte order sorted out once."""
    values = array(code)
    usable = len(raw) - (len(raw) % values.itemsize)
    values.frombytes(raw[:usable])
    if (f.order == "<") != _NATIVE_LITTLE:
        values.byteswap()
    return values


def _floats_at(f: BlendFile, block: Optional[Block], stride: int, offset: int,
               count: int, wide: int = 3) -> Optional[array]:
    """`count` runs of `wide` floats, every `stride` bytes, starting at `offset`.

    Both mesh shapes end up here. The 4.0 layer is a flat run of floats, which
    is the fast case — one read and no striding at all; the legacy `MVert` is
    the same floats with a flag and a pad between them.
    """
    if block is None:
        return None
    raw = f.bytes_of(block)
    if stride % 4 or offset % 4:
        return None
    numbers = _typed(f, raw, "f")
    step = stride // 4
    start = offset // 4
    if step == wide and start == 0:
        if len(numbers) < count * wide:
            return None
        return numbers[:count * wide]
    if len(numbers) < (count - 1) * step + start + wide:
        return None
    out = array("f")
    for index in range(count):
        base = index * step + start
        out.extend(numbers[base:base + wide])
    return out


def _ints_at(f: BlendFile, block: Optional[Block], stride: int, offset: int,
             count: int) -> Optional[array]:
    if block is None:
        return None
    raw = f.bytes_of(block)
    if stride % 4 or offset % 4:
        return None
    numbers = _typed(f, raw, "i")
    step = stride // 4
    start = offset // 4
    if step == 1 and start == 0:
        if len(numbers) < count:
            return None
        return numbers[:count]
    if len(numbers) < (count - 1) * step + start + 1:
        return None
    out = array("i")
    for index in range(count):
        out.append(numbers[index * step + start])
    return out


class Shape:
    """One mesh datablock, read: positions, faces, corners, smoothness, UVs.

    Read once even where several objects stand on it, because a file that
    scatters one rock forty times holds one rock.
    """

    __slots__ = ("positions", "faces", "corners", "smooth", "vertices",
                 "uvs", "slots")

    def __init__(self, positions: array, faces: List[Tuple[int, int]],
                 corners: array, smooth: Optional[List[bool]],
                 uvs: Optional[array] = None,
                 slots: Optional[List[int]] = None):
        self.positions = positions
        #: `(first corner, how many)` a face, already bounds-checked.
        self.faces = faces
        self.corners = corners
        #: Per face, or None where the file says nothing and everything is flat.
        self.smooth = smooth
        #: Two floats a *corner*, not a vertex — which is the whole point of a
        #: UV map: the two corners either side of a seam sit on one vertex and
        #: read opposite edges of the picture.
        self.uvs = uvs
        #: Which material slot each face uses, or None where there is one.
        self.slots = slots
        self.vertices = len(positions) // 3

    @property
    def triangles(self) -> int:
        return sum(max(0, count - 2) for _, count in self.faces)


def read_shape(f: BlendFile, block: Block) -> Optional[Shape]:
    """A `Mesh` block's geometry, by whichever route this file stores it."""
    counts = catalog.mesh_counts(f, block)
    if not counts or not counts.storage:
        return None

    shape = f.struct_of(block)
    if shape is None:
        return None

    positions = _positions(f, block, shape, counts)
    if positions is None or len(positions) < counts.vertices * 3:
        return None
    corners = _corners(f, block, shape, counts)
    if corners is None or len(corners) < counts.corners:
        return None
    faces = _faces(f, block, shape, counts)
    if not faces:
        return None

    # The smooth flags and the material a face uses are read against the file's
    # own face numbering, so they are read before anything is thrown away and
    # thinned alongside it.
    flags = _smooth(f, block, counts.storage, len(faces))
    which = _slots(f, block, counts, len(faces))
    uvs = _uvs(f, block, counts)

    # A corner index out of range is a file this reader has no business
    # guessing at, and one bad face is not a reason to lose the mesh: the face
    # is dropped and the rest is drawn.
    limit = counts.vertices
    kept: List[Tuple[int, int]] = []
    smooth: List[bool] = []
    slots: List[int] = []
    for index, (start, length) in enumerate(faces):
        if length < 3 or start < 0 or start + length > len(corners):
            continue
        if any(not 0 <= corners[start + i] < limit for i in range(length)):
            continue
        kept.append((start, length))
        if flags is not None:
            smooth.append(flags[index] if index < len(flags) else False)
        if which is not None:
            slots.append(which[index] if index < len(which) else 0)
    if not kept:
        return None

    return Shape(positions, kept, corners,
                 smooth if flags is not None else None,
                 uvs, slots if which is not None else None)


def _uvs(f, block, counts) -> Optional[array]:
    """Where each face corner reads the picture, turned the right way up.

    Blender writes the second coordinate running up from the bottom and every
    picture is drawn from the top, so it is turned over here — once, where the
    file is read, rather than left for the host to wonder about.
    """
    found = catalog.attribute_by_type(f, block, "ldata", catalog.ATTR_FLOAT2)
    if found is not None:
        raw = _floats_at(f, found[0], 8, 0, counts.corners, wide=2)
    else:
        layer = catalog.layer_by_struct(f, block, "ldata", "MLoopUV", "vec2f")
        if layer is None:
            return None
        # `MLoopUV` keeps a flag beside its two floats and `vec2f` does not, so
        # the stride is the struct's own size rather than eight.
        raw = _floats_at(f, layer[0], layer[2], 0, counts.corners, wide=2)
    if raw is None or len(raw) < counts.corners * 2:
        return None
    for at in range(1, len(raw), 2):
        raw[at] = 1.0 - raw[at]
    return raw


def _slots(f, block, counts, total: int) -> Optional[List[int]]:
    """Which material slot each face uses, where more than one is in play.

    A picture is one drawing call, so a mesh of two materials has to arrive as
    two meshes. Cutting it here rather than in the host keeps the host's list
    of meshes the only thing it has to know about.
    """
    if total <= 0:
        return None
    found = catalog.array_of(f, block, "pdata", "material_index")
    if found is not None:
        numbers = _ints_at(f, found[0], 4, 0, total)
        if numbers is not None:
            return list(numbers)

    poly = f.sdna.struct("MPoly")
    slot = poly.field("mat_nr") if poly else None
    if poly is not None and slot is not None:
        target = f.follow(block, "mpoly", "MPoly")
        if target is not None:
            raw = f.bytes_of(target)
            order = "little" if f.order == "<" else "big"
            if len(raw) >= (total - 1) * poly.size + slot.offset + 2:
                return [int.from_bytes(
                    raw[index * poly.size + slot.offset:
                        index * poly.size + slot.offset + 2], order, signed=True)
                    for index in range(total)]
    return None


def _positions(f, block, shape, counts) -> Optional[array]:
    """Where the vertices are, by whichever of the two stores this mesh uses.

    Chosen per array rather than once for the mesh, because a file in the
    middle of the change can hold one mesh of each shape.
    """
    layer = catalog.array_of(f, block, "vdata", "position")
    if layer is not None:
        found = _floats_at(f, layer[0], 12, 0, counts.vertices)
        if found is not None:
            return found
    vert = f.sdna.struct("MVert")
    held = f.follow(block, "mvert", "MVert")
    if vert is None or held is None:
        return None
    co = vert.field("co")
    if co is None:
        return None
    # `MVert` is 20 bytes in one Blender and 16 in another. Neither number is
    # written down here: the file's own DNA says which, every time. And the
    # block is asked for *as an MVert array*, because the address alone may
    # name more than one block.
    return _floats_at(f, held, vert.size, co.offset, counts.vertices)


def _corners(f, block, shape, counts) -> Optional[array]:
    layer = catalog.array_of(f, block, "ldata", ".corner_vert")
    if layer is not None:
        found = _ints_at(f, layer[0], 4, 0, counts.corners)
        if found is not None:
            return found
    loop = f.sdna.struct("MLoop")
    held = f.follow(block, "mloop", "MLoop")
    if loop is None or held is None:
        return None
    vertex = loop.field("v")
    if vertex is None:
        return None
    return _ints_at(f, held, loop.size, vertex.offset, counts.corners)


def _faces(f, block, shape, counts) -> List[Tuple[int, int]]:
    """Where each face starts and how long it is.

    4.0 writes N+1 offsets and a face is the gap between two of them; before
    that each `MPoly` carried its own start and length. The name of the offset
    array moved too — `poly_offset_indices` in 4.3, `face_offset_indices`
    later — so it is asked for by both names rather than by version.

    **Which of the two to read is not this function's decision to make.** A 3.6
    file declares `poly_offset_indices` in its DNA and leaves a stale pointer
    in it, and that pointer resolves — onto somebody else's block, whose floats
    read as face offsets in the billions. So the store is chosen once, where
    the positions were found, and the offsets are checked against the corner
    count besides.
    """
    named = shape.first("face_offset_indices", "poly_offset_indices")
    if named:
        offsets = _ints_at(f, f.follow(block, named), 4, 0, counts.faces + 1)
        # An offset array that starts at zero, never goes back and ends on the
        # corner count is an offset array. One that does not is whatever block
        # a stale pointer landed on, and there is no reading it.
        if offsets is not None and _rising(offsets, counts.corners):
            return [(offsets[i], offsets[i + 1] - offsets[i])
                    for i in range(counts.faces)]

    poly = f.sdna.struct("MPoly")
    held = f.follow(block, "mpoly", "MPoly")
    if poly is None or held is None:
        return []
    start = poly.field("loopstart")
    length = poly.field("totloop")
    if start is None or length is None:
        return []
    starts = _ints_at(f, held, poly.size, start.offset, counts.faces)
    lengths = _ints_at(f, held, poly.size, length.offset, counts.faces)
    if starts is None or lengths is None:
        return []
    return list(zip(starts, lengths))


def _rising(offsets, corners: int) -> bool:
    """Whether an offset array is one: from zero, never going back, ending right.

    Three comparisons that cost nothing and are the difference between drawing
    a mesh and drawing whatever block the pointer happened to land on.
    """
    if len(offsets) < 2 or offsets[0] != 0 or offsets[-1] != corners:
        return False
    previous = 0
    for value in offsets:
        if value < previous:
            return False
        previous = value
    return True


def _smooth(f, block, storage: str, total: int) -> Optional[List[bool]]:
    """Which faces are drawn smooth, one per face in the file's own numbering.

    Worth reading rather than assuming, and the assumption is wrong either way
    round: flat everywhere makes a character look like cut glass, smooth
    everywhere rounds the edges off a weapon.

    4.0 stores the opposite of it — a `sharp_face` layer of one byte a face —
    and a file of that shape with no such layer is smooth throughout, which is
    a real answer and not a missing one. Before that the bit lived in
    `MPoly.flag`. Returning None means the file did not say, and everything is
    then shaded flat rather than guessed smooth.
    """
    if total <= 0:
        return None

    layer = catalog.array_of(f, block, "pdata", "sharp_face")
    if layer is not None:
        target = layer[0]
        if target is not None:
            raw = f.bytes_of(target)
            if len(raw) >= total:
                return [not raw[index] for index in range(total)]

    poly = f.sdna.struct("MPoly")
    flag = poly.field("flag") if poly else None
    if poly is not None and flag is not None:
        target = f.follow(block, "mpoly", "MPoly")
        if target is not None:
            raw = f.bytes_of(target)
            if len(raw) >= (total - 1) * poly.size + flag.offset + 1:
                return [bool(raw[index * poly.size + flag.offset] & ME_SMOOTH)
                        for index in range(total)]

    # A mesh stored the 4.0 way carries the sharpness it has *away* from the
    # default, so one with the layers but no `sharp_face` among them is smooth
    # throughout. That is an answer, not a gap.
    if storage == "layers":
        return [True] * total
    return None


def _normalise(x: float, y: float, z: float) -> Tuple[float, float, float]:
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-20:
        return 0.0, 1.0, 0.0
    return x / length, y / length, z / length


class Builder:
    """Triangles, with a vertex shared wherever sharing is honest.

    A smooth face's corners share one vertex per source vertex, because that is
    what smooth means. A flat face's do not — its corners carry the face's own
    normal, and sharing them would smooth the very edge the file asked to keep.

    **A seam is the third case and it is the one that bites.** Two corners of a
    smooth mesh can sit on one vertex, face the same way, and read opposite
    edges of the picture; that is what an unwrapping seam *is*. Sharing those
    drags the whole texture across the model, so where there are UVs they are
    part of what makes two corners the same corner.
    """

    __slots__ = ("positions", "normals", "uvs", "indices", "shared")

    def __init__(self):
        self.positions: List[float] = []
        self.normals: List[float] = []
        self.uvs: List[float] = []
        self.indices: List[int] = []
        self.shared: Dict[tuple, int] = {}

    def shared_corner(self, key: tuple, point, normal, uv) -> int:
        found = self.shared.get(key)
        if found is not None:
            return found
        at = self.add(point, normal, uv)
        self.shared[key] = at
        return at

    def add(self, point, normal, uv) -> int:
        at = len(self.positions) // 3
        self.positions.extend(point)
        self.normals.extend(normal)
        if uv is not None:
            self.uvs.extend(uv)
        return at

    def triangle(self, a: int, b: int, c: int) -> None:
        self.indices.extend((a, b, c))


def build(shape: Shape, placement: List[float]) -> Builder:
    """One mesh, placed into the world and cut into triangles.

    An n-gon becomes a fan, which is what Blender's own exporters do and is
    right for every convex face and acceptable for the rest.
    """
    positions = shape.positions
    corners = shape.corners
    m = placement

    # Every vertex through the object's matrix once, rather than once per
    # corner it appears in: a quad mesh names each vertex four times.
    placed = [0.0] * (shape.vertices * 3)
    for index in range(shape.vertices):
        at = index * 3
        x, y, z = positions[at], positions[at + 1], positions[at + 2]
        wx = m[0] * x + m[4] * y + m[8] * z + m[12]
        wy = m[1] * x + m[5] * y + m[9] * z + m[13]
        wz = m[2] * x + m[6] * y + m[10] * z + m[14]
        placed[at], placed[at + 1], placed[at + 2] = to_y_up(wx, wy, wz)

    faces = shape.faces
    smooth = shape.smooth
    normals = [0.0] * (shape.vertices * 3)
    face_normals: List[Tuple[float, float, float]] = []

    for index, (start, length) in enumerate(faces):
        # Newell's, because a face normal taken off the first three corners is
        # wrong whenever those three are collinear — which happens on real
        # n-gons often enough to leave black facets across a model.
        nx = ny = nz = 0.0
        previous = corners[start + length - 1] * 3
        for step in range(length):
            current = corners[start + step] * 3
            ax, ay, az = placed[previous], placed[previous + 1], placed[previous + 2]
            bx, by, bz = placed[current], placed[current + 1], placed[current + 2]
            nx += (ay - by) * (az + bz)
            ny += (az - bz) * (ax + bx)
            nz += (ax - bx) * (ay + by)
            previous = current
        normal = _normalise(nx, ny, nz)
        face_normals.append(normal)
        if smooth is None or smooth[index]:
            for step in range(length):
                at = corners[start + step] * 3
                normals[at] += normal[0]
                normals[at + 1] += normal[1]
                normals[at + 2] += normal[2]

    uvs = shape.uvs
    slots = shape.slots
    builders: Dict[int, Builder] = {}
    for index, (start, length) in enumerate(faces):
        is_smooth = smooth is None or smooth[index]
        slot = slots[index] if slots is not None else 0
        builder = builders.get(slot)
        if builder is None:
            builder = builders[slot] = Builder()
        fan = []
        for step in range(length):
            corner = start + step
            source = corners[corner]
            at = source * 3
            point = (placed[at], placed[at + 1], placed[at + 2])
            uv = (uvs[corner * 2], uvs[corner * 2 + 1]) if uvs is not None else None
            if is_smooth:
                fan.append(builder.shared_corner(
                    (source, uv), point,
                    _normalise(normals[at], normals[at + 1], normals[at + 2]),
                    uv))
            else:
                fan.append(builder.add(point, face_normals[index], uv))
        for step in range(1, length - 1):
            builder.triangle(fan[0], fan[step], fan[step + 1])
    return builders


def meshes(f: BlendFile, max_triangles: int = MAX_TRIANGLES):
    """Every drawable mesh in the file, placed, in the host's own shape.

    Objects are walked rather than mesh datablocks, because where an object
    stands is the object's business and one mesh may be stood in forty places.
    A mesh datablock no object stands on is not in the scene and is not drawn.
    """
    cache: Dict[int, List[float]] = {}
    shapes: Dict[int, Optional[Shape]] = {}
    surfaces_of: Dict[int, list] = {}
    out: List[dict] = []
    total = 0
    dropped = 0
    held = 0
    linked = 0

    for block in f.of_code(b"OB"):
        if f.value(block, "type", default=-1) != OB_MESH:
            continue
        target = f.at_address(f.pointer(block, "data", default=0) or 0)
        if target is None:
            continue
        if target.code != b"ME\0\0":
            # An `ID` placeholder: the mesh is in another file. Counted so the
            # view can say so, never drawn and never called an empty mesh.
            linked += 1
            continue

        shape = shapes.get(target.address, False)
        if shape is False:
            shape = read_shape(f, target)
            shapes[target.address] = shape
        if shape is None:
            continue

        held += shape.triangles
        if total + shape.triangles > max_triangles:
            dropped += 1
            continue

        name = catalog.block_name(f, block)
        surfaces = surfaces_of.get(target.address)
        if surfaces is None:
            surfaces = surfaces_of[target.address] = [
                shading.surface_of(f, slot)
                for slot in shading.slots_of(f, target)]

        builders = build(shape, world_matrix(f, block, cache))
        for slot in sorted(builders):
            builder = builders[slot]
            if not builder.indices:
                continue
            surface = surfaces[slot] if 0 <= slot < len(surfaces) else None
            total += len(builder.indices) // 3
            out.append({
                # Named for the object, and for the material too where the
                # mesh was cut by one — otherwise two rows in the host's list
                # would carry the same name and mean different halves.
                "name": name if len(builders) < 2 or surface is None
                        else "%s · %s" % (name, surface["name"] or slot),
                "color": surface["color"] if surface else "",
                "picture": surface["picture"] if surface else None,
                "positions": builder.positions,
                "normals": builder.normals,
                "uvs": builder.uvs,
                "indices": builder.indices,
            })

    return out, {"triangles": total, "held": max(held, total),
                 "droppedMeshes": dropped, "linked": linked}


def skeletons(f: BlendFile) -> List[dict]:
    """Every rig in the file, as a mesh with no triangles and bones on it.

    For the file with no mesh in it — a rig and its actions, which is what an
    animation file is. Where a bone is at a moment of an action is step 3 of
    the specification and is not read: the rig is drawn standing still, and
    there are two places to read that from.

    **The pose first.** Where each bone stood when the file was saved is on the
    object, as `pose_head` and `pose_tail` of its pose channels, in the rig's
    own space. **The bones themselves are often not in the file**: an animation
    file overrides the rig of a character linked in from elsewhere, so the
    armature is an `ID` placeholder and its bones are in the library — but the
    pose belongs to the object, and the object is here. Where there is no pose,
    or one that was never worked out, the armature's own `arm_head` and
    `arm_tail` give the rest position.

    **A Blender bone is a segment, not a point**, so it is sent as its two
    ends: the tail hangs from the head, and the head from the parent's tail —
    which draws a bone that is not connected to its parent with the line
    Blender draws dashed. A connected bone's head *is* its parent's tail, and
    is sent once.
    """
    cache: Dict[int, List[float]] = {}
    out: List[dict] = []
    for block in f.of_code(b"OB"):
        if f.value(block, "type", default=-1) != OB_ARMATURE:
            continue
        segments = _posed(f, block)
        posed = bool(segments)
        if not posed:
            segments = _resting(f, block)
        if not segments:
            continue
        m = world_matrix(f, block, cache)

        def place(x: float, y: float, z: float) -> Tuple[float, float, float]:
            return to_y_up(m[0] * x + m[4] * y + m[8] * z + m[12],
                           m[1] * x + m[5] * y + m[9] * z + m[13],
                           m[2] * x + m[6] * y + m[10] * z + m[14])

        ends, parents, bones = _joined(segments, place)
        out.append({
            "name": catalog.block_name(f, block),
            "color": "",
            "picture": None,
            "positions": [],
            "normals": [],
            "uvs": [],
            "indices": [],
            "bones": ends,
            "boneParents": parents,
            "boneCount": bones,
            "posed": posed,
        })
    return out


def _posed(f: BlendFile, block: Block) -> List[tuple]:
    """A rig's bones where the file was saved with them, off its pose."""
    pose = f.follow(block, "pose", "bPose")
    if pose is None:
        return []
    channels = shading.listbase(f, pose, "chanbase", "bPoseChannel",
                                most=MOST_ENDS)
    slot = {channel.at: index for index, channel in enumerate(channels)}
    raw = []
    for channel in channels:
        head = f.numbers(channel, "pose_head", 3)
        tail = f.numbers(channel, "pose_tail", 3)
        if len(head) < 3 or len(tail) < 3:
            return []
        parent = f.follow(channel, "parent", "bPoseChannel")
        raw.append((head, tail,
                    slot.get(parent.at, -1) if parent is not None else -1))
    # A pose nobody ever worked out is all zeros, and would draw the whole rig
    # as one dot at its origin. The rest position is the better answer then.
    if not any(value for head, tail, _ in raw for value in head + tail):
        return []
    return _parents_first(raw)


def _resting(f: BlendFile, block: Block) -> List[tuple]:
    """A rig's bones at rest, off the armature — where the armature is here."""
    rig = f.follow(block, "data", "bArmature")
    if rig is None:
        return []
    out: List[tuple] = []
    # Walked with a stack rather than by recursion: a tail or a chain of spine
    # bones is as deep as its author made it.
    waiting = [(bone, -1) for bone in
               reversed(shading.listbase(f, rig, "bonebase", "Bone"))]
    seen = set()
    while waiting and len(out) < MOST_ENDS:
        bone, above = waiting.pop()
        if bone.at in seen:
            continue
        seen.add(bone.at)
        head = f.numbers(bone, "arm_head", 3)
        tail = f.numbers(bone, "arm_tail", 3)
        if len(head) < 3 or len(tail) < 3:
            continue
        here = len(out)
        out.append((head, tail, above))
        for child in reversed(shading.listbase(f, bone, "childbase", "Bone")):
            waiting.append((child, here))
    return out


def _parents_first(raw: List[tuple]) -> List[tuple]:
    """Segments put in an order where each comes after the one it hangs from.

    A pose is a flat list of channels with a pointer to the parent on each, and
    nothing promises the parent is listed first. A cycle, which Blender does
    not write, is dropped rather than followed.
    """
    children: Dict[int, List[int]] = {}
    roots: List[int] = []
    for index, (_head, _tail, parent) in enumerate(raw):
        if 0 <= parent < len(raw) and parent != index:
            children.setdefault(parent, []).append(index)
        else:
            roots.append(index)
    out: List[tuple] = []
    placed: Dict[int, int] = {}
    waiting = list(reversed(roots))
    while waiting:
        index = waiting.pop()
        if index in placed:
            continue
        head, tail, parent = raw[index]
        placed[index] = len(out)
        out.append((head, tail, placed.get(parent, -1)))
        waiting.extend(reversed(children.get(index, [])))
    return out


def _joined(segments: List[tuple], place) -> Tuple[List[float], List[int], int]:
    """Segments as the ends the host draws, a connected head sent once.

    Each segment names the one it hangs from by its place in the list, and that
    place is always earlier — both readers above make sure of it.
    """
    ends: List[float] = []
    parents: List[int] = []
    tails: List[int] = []
    for head, tail, above in segments:
        if len(parents) >= MOST_ENDS - 1:
            break
        if above >= 0 and all(abs(a - b) < 1e-4
                              for a, b in zip(head, segments[above][1])):
            start = tails[above]
        else:
            start = len(parents)
            ends.extend(place(*head))
            parents.append(tails[above] if above >= 0 else -1)
        tails.append(len(parents))
        ends.extend(place(*tail))
        parents.append(start)
    return ends, parents, len(tails)


def pack_floats(values) -> bytes:
    return struct.pack("<%df" % len(values), *values)


def pack_indices(values) -> bytes:
    return struct.pack("<%dI" % len(values), *values)
