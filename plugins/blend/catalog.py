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

"""What the blocks mean: the table of contents, and the counting behind it.

Everything here asks the file's own DNA what exists rather than asking what
version wrote it. That is not a nicety — `Mesh` is laid out differently in
2.92 and 4.4, and a reader that switched on the version would need a table of
every release and would still be wrong about the next one.

The one thing this module is careful about beyond counting is **saying why a
file has nothing to draw**, because two files in ten are that file and the
difference between a useful tool and a lying one is right there.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from blendfile import Block, BlendFile

#: `Object.type`. Blender's own `OB_*` constants; a number not here is shown as
#: itself rather than guessed at.
OBJECT_KINDS = {
    0: "Empty",
    1: "Mesh",
    2: "Curve",
    3: "Surface",
    4: "Text",
    5: "Metaball",
    10: "Light",
    11: "Camera",
    12: "Speaker",
    13: "Light probe",
    22: "Lattice",
    25: "Armature",
    26: "Grease pencil",
    27: "Curves",
    28: "Point cloud",
    29: "Volume",
    30: "Grease pencil",
}

#: The two-letter code a datablock is written under, in English. Only the ones
#: worth a line in a table of contents; the rest are counted as "other".
DATABLOCKS = [
    (b"SC", "Scenes"),
    (b"OB", "Objects"),
    (b"ME", "Meshes"),
    (b"CU", "Curves"),
    (b"MB", "Metaballs"),
    (b"AR", "Armatures"),
    (b"AC", "Actions"),
    (b"MA", "Materials"),
    (b"IM", "Images"),
    (b"TE", "Textures"),
    (b"NT", "Node trees"),
    (b"GR", "Collections"),
    (b"CA", "Cameras"),
    (b"LA", "Lights"),
    (b"WO", "Worlds"),
    (b"KE", "Shape keys"),
    (b"LT", "Lattices"),
    (b"VF", "Fonts"),
    (b"SO", "Sounds"),
    (b"TX", "Texts"),
    (b"GD", "Grease pencil"),
    (b"LI", "Libraries"),
]

#: Blocks that are the application rather than the work: the window layout, the
#: brushes a fresh install ships, the undo scratch. Counting them in a table of
#: contents tells the reader nothing about their file.
FURNITURE = {b"WM\0\0", b"WS\0\0", b"SN\0\0", b"SR\0\0", b"BR\0\0",
             b"PL\0\0", b"DATA", b"DNA1", b"GLOB", b"REND", b"TEST",
             b"USER", b"ENDB"}


def _code(text: bytes) -> bytes:
    return text.ljust(4, b"\0")


def block_name(f: BlendFile, block: Block) -> str:
    """What a datablock is called, or a plain stand-in when it is nameless."""
    return f.name_of(block) or "(unnamed)"


# -- meshes ----------------------------------------------------------------

class MeshCounts:
    """How much geometry a `Mesh` block holds, and where it keeps it.

    `storage` is the reason this is a class rather than a tuple: the two ways
    a `.blend` stores a mesh both appear in one folder of the user's files, and
    the step that draws them needs to be told which without asking the version
    a second time.
    """

    __slots__ = ("vertices", "faces", "corners", "edges", "storage")

    def __init__(self, vertices: int, faces: int, corners: int, edges: int,
                 storage: str):
        self.vertices = vertices
        self.faces = faces
        self.corners = corners
        self.edges = edges
        #: `"layers"` for 4.0's generic attributes, `"legacy"` for `MVert` and
        #: friends, `""` where the mesh holds nothing drawable.
        self.storage = storage

    @property
    def triangles(self) -> int:
        """**A count, not a formula that looks like one.**

        Triangulating an n-gon by a fan gives `n - 2` triangles, so the total
        is `corners - 2 * faces`. `corners - faces` is the tempting wrong
        answer and overstates a quad mesh by a full triangle a face — it
        inflated two of the measured files by about 70% and produced a false
        conclusion that the renderer needed decimation.
        """
        return max(0, self.corners - 2 * self.faces)

    def __bool__(self) -> bool:
        return self.vertices > 0 and self.faces > 0


#: Which domain a `CustomData` member stands for, in the numbering Blender 5
#: uses for the same thing. Vertices, edges, faces, face corners.
DOMAINS = {"vdata": 0, "edata": 1, "pdata": 2, "ldata": 3}

#: `Attribute.storage_type`. An array has a value each; a single has one value
#: standing for all of them, which is how a mesh that is smooth throughout says
#: so in no space at all.
STORED_ARRAY = 0
STORED_SINGLE = 1


def attribute_of(f: BlendFile, block: Block, domain: str,
                 *names: str) -> Optional[Tuple[int, int]]:
    """A named attribute on a Blender 5 mesh: where its data is, and how much.

    Blender 5 replaced the four `CustomData` members with one
    `attribute_storage` holding every attribute of every domain in a single
    list, each with its name, its domain and a pointer to either an array of
    values or a single value standing for all of them.

    The names did not change — `position`, `.corner_vert` — which is the whole
    reason one reader still serves 2.91 and 5.2: it has always asked for the
    thing by name.

    Returns the address of the raw values and how many there are, or a count of
    0 where one value stands for every element.
    """
    shape = f.sdna.struct("Attribute")
    if shape is None:
        return None
    wanted_domain = DOMAINS.get(domain)
    if wanted_domain is None:
        return None

    total = f.value(block, "attribute_storage.dna_attributes_num", default=0) or 0
    if total <= 0:
        return None
    holder = f.follow(block, "attribute_storage.dna_attributes", "Attribute")
    if holder is None:
        return None

    name_field = shape.field("name")
    domain_field = shape.field("domain")
    stored_field = shape.field("storage_type")
    data_field = shape.field("data")
    if not all((name_field, domain_field, stored_field, data_field)):
        return None

    raw = f.bytes_of(holder)
    # The count in the struct and the count on the block have disagreed by one
    # on every file measured; the block is the one that cannot overrun.
    limit = min(total, holder.count or total, len(raw) // shape.size)
    wanted = set(names)
    for index in range(limit):
        base = index * shape.size
        if raw[base + domain_field.offset] != wanted_domain:
            continue
        if _string_at(f, raw, base + name_field.offset, holder.at) not in wanted:
            continue
        stored = raw[base + stored_field.offset]
        address = _address_at(f, raw, base + data_field.offset)
        # Every one of these is a block written after the one pointing at it,
        # and every one of these addresses is reused by the next mesh, so the
        # search is anchored each time on where the pointer came from.
        wrapper = "AttributeSingle" if stored == STORED_SINGLE else "AttributeArray"
        found = f.at_address(address, wrapper, after=holder.at)
        if found is None:
            return None
        inner = f.follow(found, "data")
        if inner is None:
            return None
        if stored == STORED_SINGLE:
            return inner, 0
        return inner, (f.value(found, "size", default=0) or 0)
    return None


def _string_at(f: BlendFile, raw: bytes, at: int, after: int = 0) -> str:
    """A `char *` inside a block: the address, then the block it names."""
    target = f.at_address(_address_at(f, raw, at), after=after or None)
    if target is None:
        return ""
    text = f.data[target.at:target.at + target.length]
    return text.split(b"\0")[0].decode("utf-8", "replace")


def _address_at(f: BlendFile, raw: bytes, at: int) -> int:
    width = f.pointer_size
    if at + width > len(raw):
        return 0
    return int.from_bytes(raw[at:at + width],
                          "little" if f.order == "<" else "big")


def array_of(f: BlendFile, block: Block, domain: str,
             *names: str) -> Optional[Tuple[Block, int]]:
    """A named run of mesh data, by whichever of the two stores holds it.

    Blender 5's attribute storage first, then 4.0's `CustomData` layers. Both
    are asked for by the same names, so everything above this is spared knowing
    which release it is looking at.
    """
    found = attribute_of(f, block, domain, *names)
    if found is not None:
        return found
    return layer_of(f, block, domain, *names)


def layer_of(f: BlendFile, block: Block, domain: str,
             *names: str) -> Optional[Tuple[int, int]]:
    """A named attribute layer on a mesh: where its data block is, and its count.

    Blender 4.0 moved positions and face corners out of named struct fields and
    into generic `CustomData` layers, found by the name the layer carries —
    `position`, `.corner_vert`. Asking for the layer by name is what lets one
    reader serve both shapes: a file that has it uses it, a file that does not
    falls back to the array its own DNA still declares.

    Returns the address of the layer's data and how many entries it holds, or
    None where there is no such layer.
    """
    shape = f.sdna.struct("CustomDataLayer")
    if shape is None:
        return None
    total = f.value(block, domain + ".totlayer", default=0) or 0
    if total <= 0:
        return None
    holder = f.follow(block, domain + ".layers", "CustomDataLayer")
    if holder is None:
        holder = f.follow(block, domain + ".layers")
    if holder is None:
        return None

    name_field = shape.field("name")
    data_field = shape.field("data")
    if name_field is None or data_field is None:
        return None
    raw = f.bytes_of(holder)
    wanted = set(names)
    for index in range(min(total, max(holder.count, 1) or total)):
        base = index * shape.size
        if base + shape.size > len(raw):
            break
        start = base + name_field.offset
        text = raw[start:start + name_field.count].split(b"\0")[0]
        if text.decode("utf-8", "replace") not in wanted:
            continue
        address = _address_at(f, raw, base + data_field.offset)
        target = f.at_address(address, after=holder.at)
        if target is None:
            # The layer is declared and its data was not written. Normal, and
            # not the same as the layer being absent: the caller must not then
            # fall back to a legacy array that is equally empty.
            return None
        return target, target.count
    return None


def mesh_counts(f: BlendFile, block: Block) -> MeshCounts:
    """What a `Mesh` block holds, whichever way this file stores it.

    The counter names moved in 4.0 and the storage moved with them, but the two
    did not move together in every release — a 4.3 file still declares
    `totvert` while keeping the positions in a `position` layer. So the count
    and the storage are asked separately, each by name.
    """
    shape = f.struct_of(block)
    if shape is None:
        return MeshCounts(0, 0, 0, 0, "")

    def count(*names: str) -> int:
        found = shape.first(*names)
        return (f.value(block, found, default=0) or 0) if found else 0

    vertices = count("verts_num", "totvert")
    faces = count("faces_num", "totpoly")
    corners = count("corners_num", "totloop")
    edges = count("edges_num", "totedge")

    # Which store is the live one, asked of the file rather than of the
    # version. **The attribute layer wins wherever it exists**: 3.6 still
    # writes an MVert array for the benefit of older Blenders, and in a file
    # that has both the legacy array is not the authoritative one.
    storage = ""
    if vertices > 0:
        if array_of(f, block, "vdata", "position"):
            storage = "layers"
        elif f.at_address(f.pointer(block, "mvert", default=0) or 0, "MVert"):
            # Asked for as an MVert array, not merely as something at that
            # address: a deprecated pointer left over from a freed buffer can
            # land on a live block of another kind entirely.
            storage = "legacy"
    return MeshCounts(vertices, faces, corners, edges, storage)


def meshes(f: BlendFile) -> List[dict]:
    """Every mesh the file itself holds, heaviest first.

    Only local ones: a mesh that lives in another file is not here to be
    counted, and is reported as a link instead.
    """
    out = []
    for block in f.of_code(b"ME"):
        counts = mesh_counts(f, block)
        out.append({
            "name": block_name(f, block),
            "vertices": counts.vertices,
            "faces": counts.faces,
            "triangles": counts.triangles,
            "storage": counts.storage,
            "block": block,
        })
    out.sort(key=lambda m: (-m["triangles"], m["name"]))
    return out


# -- what is only referenced ------------------------------------------------

def linked(f: BlendFile) -> dict:
    """Objects whose data lives in another file, and the files they name.

    **A pointer that resolves is not data.** In a linked-library file every
    mesh object's `data` pointer resolves perfectly — to an `ID` placeholder
    block, which is Blender's note that the real datablock is somewhere else.
    A first pass that trusted the pointer counted 102 of those as local meshes
    and reported 102 meshes with no vertices in them.

    So the code of the block a pointer lands on is checked, always.
    """
    by_kind: Dict[str, int] = {}
    total = 0
    for block in f.of_code(b"OB"):
        address = f.pointer(block, "data", default=0) or 0
        if not address:
            continue
        target = f.at_address(address)
        if target is None or target.code != _code(b"ID"):
            continue
        kind = OBJECT_KINDS.get(f.value(block, "type", default=-1), "Other")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        total += 1

    libraries = []
    for block in f.of_code(b"LI"):
        # `name` is the path as the file writes it, which is what a reader
        # wants to see. It is Blender's own relative form — `//` meaning
        # "beside this file" — and its separators are whatever the authoring
        # machine used, so it is shown and never resolved.
        path = f.string(block, "name") or block_name(f, block)
        if path:
            libraries.append(path)
    return {"objects": total, "byKind": by_kind, "libraries": libraries}


# -- the whole answer -------------------------------------------------------

def objects_by_kind(f: BlendFile) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for block in f.of_code(b"OB"):
        kind = f.value(block, "type", default=-1)
        name = OBJECT_KINDS.get(kind, "Type %d" % kind)
        out[name] = out.get(name, 0) + 1
    return out


def datablocks(f: BlendFile) -> List[Tuple[str, int]]:
    """What the file holds, by kind, in a fixed order and without the furniture."""
    counts = f.codes()
    out = []
    for code, label in DATABLOCKS:
        found = counts.get(_code(code), 0)
        if found:
            out.append((label, found))
    return out


def placed(f: BlendFile, local: List[dict]) -> dict:
    """What actually stands in the scene, as against what the file stores.

    The two are not the same number and the difference is not a fault in
    either. A mesh datablock is stored once and may be stood in forty places —
    a bolt, a paving slab, a leaf — so a file holding nine thousand triangles
    can put a hundred and thirty thousand in front of the camera. The table of
    meshes counts what is stored; the model view draws what stands. Reporting
    only one of them makes the other look like a bug.
    """
    by_address = {m["block"].address: m for m in local}
    objects = 0
    triangles = 0
    for block in f.of_code(b"OB"):
        if f.value(block, "type", default=-1) != 1:
            continue
        target = f.at_address(f.pointer(block, "data", default=0) or 0)
        if target is None or target.code != _code(b"ME"):
            continue
        mesh = by_address.get(target.address)
        if mesh is None or mesh["triangles"] <= 0:
            continue
        objects += 1
        triangles += mesh["triangles"]
    return {"objects": objects, "triangles": triangles}


def summarise(f: BlendFile) -> dict:
    """Everything the table of contents shows, counted once."""
    local = meshes(f)
    drawable = [m for m in local if m["triangles"] > 0]
    standing = placed(f, local)
    return {
        "placed": standing,
        "version": f.version,
        "pointerSize": f.pointer_size,
        "bigEndian": f.order == ">",
        "compressed": f.compressed,
        "truncated": f.truncated,
        "blocks": len(f.blocks),
        "structs": len(f.sdna.structs),
        "objects": objects_by_kind(f),
        "meshes": local,
        "triangles": sum(m["triangles"] for m in drawable),
        "vertices": sum(m["vertices"] for m in drawable),
        "linked": linked(f),
        "datablocks": datablocks(f),
        "scenes": [block_name(f, b) for b in f.of_code(b"SC")],
        "actions": [block_name(f, b) for b in f.of_code(b"AC")],
        "armatures": [block_name(f, b) for b in f.of_code(b"AR")],
    }
