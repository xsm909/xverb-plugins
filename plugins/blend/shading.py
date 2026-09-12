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

"""What a material is painted with: the node walk, and the picture at the end.

**The colour is not in the colour fields.** `Material.r/g/b` are right there and
are the default 0.80 grey on very nearly every material in a real file, because
what the material actually looks like lives in a node tree. So the tree is
walked: find the Principled BSDF, take the link into its *Base Color* input, and
follow it back to the image node feeding it.

Which link matters, and taking any image node instead of that one is the
difference between a model and a mistake — a character here carries five
pictures for one material, of which *color* is one and specular, roughness,
normal and metallic are the other four. Painting a face with its roughness map
is not a smaller version of being right.

Where no picture feeds the socket, the socket's own default colour is read, and
only where there is no tree at all does `Material.r/g/b` get used.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from blendfile import Block, BlendFile

#: The node that says what a surface looks like. Blender has had others, and a
#: file may use any of them, so the walk falls back to whatever feeds the
#: material output rather than insisting on this one.
PRINCIPLED = "ShaderNodeBsdfPrincipled"
TEX_IMAGE = "ShaderNodeTexImage"
OUTPUT = "ShaderNodeOutputMaterial"

#: What the socket carrying the visible colour is called on each of the nodes
#: worth reading. Blender renamed Principled's from `Base Color` to `Base
#: Color` only in capitalisation over the years, so both spellings are taken.
COLOUR_SOCKETS = ("Base Color", "Base color", "Color", "Colour")

#: How far back through the tree a colour is chased. A picture plugged straight
#: in is one step; through a mix, a gamma or a colour ramp is two or three. Past
#: that it is a shader graph rather than a texture, and guessing is worse than
#: the material colour.
DEPTH = 4


def listbase(f: BlendFile, block: Block, path: str,
             kind: str, most: int = 4000) -> List[Block]:
    """A Blender `ListBase` walked into a list of blocks.

    `first`, then `next` on each. Guarded on both length and on revisiting a
    block, because this reader is pointed at files it did not write.
    """
    out: List[Block] = []
    seen = set()
    node = f.follow(block, path + ".first", kind) or f.follow(block, path + ".first")
    while node is not None and node.address not in seen and len(out) < most:
        seen.add(node.address)
        out.append(node)
        node = f.follow(node, "next", kind) or f.follow(node, "next")
    return out


def _socket_named(f: BlendFile, node: Block, names) -> Optional[Block]:
    for socket in listbase(f, node, "inputs", "bNodeSocket", most=64):
        if f.string(socket, "name") in names:
            return socket
    return None


def _rgba(f: BlendFile, socket: Block) -> str:
    """A socket's own colour, where nothing is plugged into it."""
    value = f.follow(socket, "default_value", "bNodeSocketValueRGBA")
    if value is None:
        return ""
    numbers = f.numbers(value, "value", 4)
    if len(numbers) < 3:
        return ""
    return _hex(numbers[0], numbers[1], numbers[2])


def _hex(r: float, g: float, b: float) -> str:
    def one(v: float) -> int:
        # Blender keeps colour in linear light and the host paints in sRGB. The
        # standard transfer curve rather than a plain gamma, because the toe at
        # the bottom is where dark materials live and 2.2 alone makes every one
        # of them noticeably too dark.
        v = max(0.0, min(1.0, v))
        v = 12.92 * v if v <= 0.0031308 else 1.055 * (v ** (1 / 2.4)) - 0.055
        return max(0, min(255, int(round(v * 255))))
    return "#%02x%02x%02x" % (one(r), one(g), one(b))


class Tree:
    """One material's node tree, indexed the two ways the walk needs it."""

    __slots__ = ("nodes", "by_socket")

    def __init__(self, f: BlendFile, tree: Block):
        self.nodes = listbase(f, tree, "nodes", "bNode")
        # A link says which socket it arrives at, so the question asked of it is
        # always "what feeds this socket" — one dictionary answers it.
        self.by_socket: Dict[int, Block] = {}
        for link in listbase(f, tree, "links", "bNodeLink"):
            to = f.pointer(link, "tosock", default=0) or 0
            source = f.follow(link, "fromnode", "bNode")
            if to and source is not None:
                self.by_socket[to] = source

    def named(self, f: BlendFile, idname: str) -> Optional[Block]:
        for node in self.nodes:
            if f.string(node, "idname") == idname:
                return node
        return None


def _image_behind(f: BlendFile, tree: Tree, socket: Block,
                  depth: int = DEPTH) -> Optional[Block]:
    """The image node feeding a socket, through whatever sits between."""
    node = tree.by_socket.get(socket.address)
    if node is None or depth <= 0:
        return None
    if f.string(node, "idname") == TEX_IMAGE:
        return node
    # Not a picture: a mix, a ramp, a gamma. Walk its own inputs, colour ones
    # first, and take the first picture found behind any of them.
    inputs = listbase(f, node, "inputs", "bNodeSocket", most=64)
    inputs.sort(key=lambda s: 0 if f.string(s, "name") in COLOUR_SOCKETS else 1)
    for inner in inputs:
        found = _image_behind(f, tree, inner, depth - 1)
        if found is not None:
            return found
    return None


def _picture_of(f: BlendFile, node: Block) -> Optional[dict]:
    """The bitmap a texture node points at: its bytes, or where to look.

    Blender either packs the picture into the `.blend` or leaves a path to it.
    Both are common in one folder — a weapon here keeps its four maps beside the
    file and a character has all fifteen of its own packed in.
    """
    image = f.follow(node, "id", "Image")
    if image is None:
        return None
    name = f.string(image, "name")
    out = {"name": name.replace("\\", "/").rsplit("/", 1)[-1] or "picture",
           "beside": name}

    held = _packed_bytes(f, image)
    if held is not None:
        out["bytes"] = held
    return out


def _packed_bytes(f: BlendFile, image: Block) -> Optional[bytes]:
    """The picture's own bytes, where the file carries them.

    Two shapes: one `packedfile` on the image, and — for a tiled image, and in
    newer files for every image — a list of them, one per tile. The first tile
    is the picture as far as a preview is concerned.
    """
    packed = f.follow(image, "packedfile", "PackedFile")
    if packed is None:
        for tile in listbase(f, image, "packedfiles", "ImagePackedFile", most=8):
            packed = f.follow(tile, "packedfile", "PackedFile")
            if packed is not None:
                break
    if packed is None:
        return None
    size = f.value(packed, "size", default=0) or 0
    if size <= 0:
        return None
    held = f.follow(packed, "data")
    if held is None:
        return None
    return f.data[held.at:held.at + min(size, held.length)]


def surface_of(f: BlendFile, material: Optional[Block]) -> dict:
    """What one material looks like: a name, a colour, and a picture if any."""
    if material is None:
        return {"name": "", "color": "", "picture": None}

    from catalog import block_name
    out = {"name": block_name(f, material), "color": "", "picture": None}

    tree_block = f.follow(material, "nodetree", "bNodeTree")
    if tree_block is None:
        # No tree at all, so the legacy fields are the whole truth rather than
        # a stale copy of it, and are worth reading.
        out["color"] = _hex(f.value(material, "r", default=0.8) or 0.0,
                            f.value(material, "g", default=0.8) or 0.0,
                            f.value(material, "b", default=0.8) or 0.0)
        return out

    tree = Tree(f, tree_block)
    shader = tree.named(f, PRINCIPLED)
    if shader is None:
        output = tree.named(f, OUTPUT)
        socket = _socket_named(f, output, ("Surface",)) if output else None
        shader = tree.by_socket.get(socket.address) if socket else None
    if shader is None:
        return out

    socket = _socket_named(f, shader, COLOUR_SOCKETS)
    if socket is None:
        return out

    node = _image_behind(f, tree, socket)
    if node is not None:
        out["picture"] = _picture_of(f, node)
        if out["picture"] is not None:
            return out
    out["color"] = _rgba(f, socket)
    return out


def slots_of(f: BlendFile, mesh: Block) -> List[Optional[Block]]:
    """A mesh's material slots, in the order its faces index them."""
    total = f.value(mesh, "totcol", default=0) or 0
    if total <= 0:
        return []
    held = f.follow(mesh, "mat")
    if held is None:
        return []
    raw = f.bytes_of(held)
    width = f.pointer_size
    order = "little" if f.order == "<" else "big"
    out: List[Optional[Block]] = []
    for index in range(min(total, len(raw) // width)):
        at = index * width
        address = int.from_bytes(raw[at:at + width], order)
        found = f.at_address(address, "Material")
        if found is None:
            candidate = f.at_address(address)
            found = candidate if (candidate is not None
                                  and candidate.code == b"MA\0\0") else None
        out.append(found)
    return out
