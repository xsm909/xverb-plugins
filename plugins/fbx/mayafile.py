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

"""Maya scenes — `.ma`, and `.mb` through :mod:`mayabinary` — read into the
same nodes, and the meshes in them built.

**A `.ma` is a MEL script.** `createNode mesh -n "pCubeShape1" -p "pCube1";`
makes a node, the `setAttr` lines after it fill it in, `connectAttr` wires one
node's output into another's input. A viewer does not run the script; it reads
those three statements and a few more (`select`, `parent -add`,
`currentUnit`, `file -r`, `requires`), and ignores the rest.

**Where a mesh's shape is, and it is not always in the mesh.** Three cases,
in the order they are tried:

1. The mesh holds it: points (`.vt`, moved by `.pt`), edges (`.ed`) and
   faces (`.fc`, each a list of edges, and the picture coordinates of its
   corners).
2. It is deformed — skinned, tweaked, blended — and the points are in the
   *original* shape upstream (`…ShapeOrig`, marked intermediate), reached
   through the deformers. The deformers are not run: the model is shown as it
   was bound, which is the pose a rig is built in.
3. It comes from a primitive — `polyCube`, `polySphere`, `polyPlane`,
   `polyCylinder`, `polyCone`, `polyTorus` — which the file does not store
   the result of. Those are rebuilt here from their few numbers, the way Maya
   builds them, down to where the seams are. A `polyTweak` on top is applied.

Anything else in the way — an extrusion, a split, a merge — is modelling
history that only Maya can replay, and such a mesh is counted and named
rather than guessed at.

**Placement.** A transform is Maya's: scale about its pivot, rotate about
its pivot in its rotate order after the rotate axis (and a joint's orient),
translate; a child is placed in its parent. The meshes go out in world space.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple

import geometry
from geometry import MeshBuilder, IDENTITY

#: Maya's rotate orders (xyz, yzx, zxy, xzy, yxz, zyx) as the FBX ones
#: :func:`geometry.rotation` speaks.
_ROTATE_ORDER = {0: 0, 1: 2, 2: 4, 3: 1, 4: 3, 5: 5}

_UNITS = {"mm": 0.1, "millimeter": 0.1, "cm": 1.0, "centimeter": 1.0,
          "m": 100.0, "meter": 100.0, "km": 100000.0, "kilometer": 100000.0,
          "in": 2.54, "inch": 2.54, "ft": 30.48, "foot": 30.48,
          "yd": 91.44, "yard": 91.44, "mi": 160934.4, "mile": 160934.4}

_RATES = {"game": 15.0, "film": 24.0, "pal": 25.0, "ntsc": 30.0, "show": 48.0,
          "palf": 50.0, "ntscf": 60.0}

#: Nodes a mesh passes through on its way from the shape that holds its
#: points to the one that is drawn, none of which change what it is made of.
_PASSING = {"groupParts", "tweak", "skinCluster", "blendShape", "cluster",
            "polyTweakUV", "polySoftEdge", "polyNormal", "polyNormalPerVertex",
            "deleteComponent", "wire", "ffd", "nonLinear", "sculpt", "wrap",
            "deltaMush", "tension", "shrinkWrap", "proximityWrap", "polyTweak",
            "transformGeometry", "polyMapCut", "polyMapSew", "polyMapDel",
            "polyAutoProj", "polyPlanarProj", "polyCylProj", "polySphProj",
            "polyProjection", "polyMergeUV", "polyLayoutUV", "polyFlipUV",
            "polyNormalizeUV", "polyCopyUV", "polyUVRectangle", "polyColorPerVertex"}

_PRIMITIVES = {"polyCube", "polySphere", "polyPlane", "polyCylinder",
               "polyCone", "polyTorus"}

#: Which attribute of a node takes the mesh coming in.
_INPUT = re.compile(r"^(?:i|in|inMesh|ip|inputPolymesh|ig|inputGeometry|"
                    r"ip\[\d+\]\.ig|input\[\d+\]\.inputGeometry)$")


class MayaError(ValueError):
    pass


class Node:
    __slots__ = ("type", "name", "parent", "attrs", "extra_parents")

    def __init__(self, kind: str, name: str, parent: Optional[str]):
        self.type = kind
        self.name = name
        self.parent = parent
        #: attribute → list of (range start or None, values) as they were set.
        self.attrs: Dict[str, list] = {}
        self.extra_parents: List[str] = []

    def values(self, attr: str) -> Optional[list]:
        found = self.attrs.get(attr.lstrip("."))
        return found[-1][1] if found else None

    def number(self, attr: str, default: float) -> float:
        found = self.values(attr)
        if not found:
            return default
        return _float(found[0], default)

    def vector(self, attr: str, default=(0.0, 0.0, 0.0)) -> Tuple[float, float, float]:
        found = self.values(attr)
        if not found or len(found) < 3:
            return default
        return (_float(found[0], default[0]), _float(found[1], default[1]),
                _float(found[2], default[2]))

    def flag(self, attr: str) -> bool:
        found = self.values(attr)
        return bool(found) and _float(found[0], 0.0) != 0.0

    def indexed(self, attr: str) -> List[Tuple[int, list]]:
        """Every `setAttr ".attr[a:b]" …` of [attr], as (first index, values)."""
        return [(start or 0, values) for start, values in self.attrs.get(attr, [])]


def _float(text, default: float = 0.0) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        if str(text).lower() in ("yes", "true", "on"):
            return 1.0
        if str(text).lower() in ("no", "false", "off"):
            return 0.0
        return default


# -- reading the script ---------------------------------------------------------

_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([^\s;"]+)|(;)', re.S)
_ATTR = re.compile(r"^(?P<node>[^.]*)\.(?P<attr>[^\[]+?)(?:\[(?P<a>-?\d+)(?::(?P<b>-?\d+))?\])?(?P<rest>(?:\..*)?)$")


class Scene:
    def __init__(self):
        self.nodes: Dict[str, Node] = {}
        #: destination "node.attr" → source "node.attr"
        self.incoming: Dict[str, str] = {}
        self.outgoing: Dict[str, List[str]] = {}
        self.version = ""
        self.creator = ""
        self.unit = "centimeter"
        self.rate = "film"
        self.up = "Y"
        self.references: List[str] = []
        self.binary = False
        self._raw: List[Tuple[str, str]] = []

    def node(self, name: str) -> Optional[Node]:
        """A node by its name or by the last part of a `|path|name`."""
        found = self.nodes.get(name)
        if found is None and "|" in name:
            found = self.nodes.get(name.rsplit("|", 1)[-1])
        return found

    def add(self, kind: str, name: str, parent: Optional[str]) -> Node:
        if parent and "|" in parent:
            parent = parent.rsplit("|", 1)[-1]
        node = Node(kind, name, parent)
        # Two nodes of one short name under different parents are written as
        # paths; kept under their short name the second would hide the first.
        key = name if name not in self.nodes else "%s|%s" % (parent or "", name)
        self.nodes[key] = node
        node.name = key
        return node

    def connect(self, source: str, target: str) -> None:
        self._raw.append((source, target))

    def settle(self) -> None:
        """The connections, with every node named as it is kept here. A file
        names a node by its path — `|pCube1|pCubeShape1.i` — wherever the
        short name alone is not unique, or just because; resolved once the
        whole file is read, since a connection may name a node made later."""
        for source, target in self._raw:
            source, target = self._short(source), self._short(target)
            self.incoming[target] = source
            self.outgoing.setdefault(source.split(".", 1)[0], []).append(target)
        self._raw = []

    def _short(self, plug: str) -> str:
        node, dot, attr = plug.partition(".")
        found = self.node(node.lstrip(":")) or self.node(node)
        return (found.name if found else node) + dot + attr

    def into(self, name: str, attr_pattern=_INPUT) -> Optional[Tuple[str, str]]:
        """What feeds [name]'s mesh input, as (node, attribute)."""
        for target, source in self.incoming.items():
            node, _, attr = target.partition(".")
            if node == name and attr_pattern.match(attr):
                src_node, _, src_attr = source.partition(".")
                return src_node, src_attr
        return None


def _set(node: Node, name: str, rest: List[str]) -> None:
    kind = None
    values = []
    i = 0
    while i < len(rest):
        word = rest[i]
        if word in ("-type", "-typ"):
            kind = rest[i + 1] if i + 1 < len(rest) else None
            i += 2
            continue
        if word in ("-s", "-size", "-l", "-lock", "-k", "-keyable", "-cb",
                    "-channelBox", "-av", "-alteredValue", "-ca", "-caching"):
            i += 2 if word not in ("-av", "-alteredValue") else 1
            continue
        if word.startswith("-") and not _is_number(word):
            i += 1
            continue
        values.append(word)
        i += 1
    found = _ATTR.match(name if name.startswith(".") or "." in name else "." + name)
    if not found:
        return
    attr = found.group("attr") + (found.group("rest") or "")
    start = int(found.group("a")) if found.group("a") is not None else None
    if kind in ("string", "stringArray"):
        values = values
    node.attrs.setdefault(attr, []).append((start, values))
    if kind:
        node.attrs.setdefault(attr + "#type", []).append((None, [kind]))


def _is_number(word: str) -> bool:
    try:
        float(word)
        return True
    except ValueError:
        return False


def parse(text: str) -> Scene:
    """The statements a viewer needs out of a Maya ASCII file."""
    scene = Scene()
    current: Optional[Node] = None
    statement: List[str] = []
    # A comment runs to the end of its line, and Maya writes them only at
    # the start of one — the first line of every file among them.
    text = re.sub(r"(?m)^[ \t]*//.*$", "", text)
    for found in _TOKEN.finditer(text):
        quoted, word, end = found.groups()
        if end is None:
            if quoted is not None:
                statement.append(quoted.replace('\\"', '"').replace("\\\\", "\\"))
            else:
                statement.append(word)
            continue
        if not statement:
            continue
        head, rest = statement[0], statement[1:]
        statement = []
        if head == "createNode" and rest:
            kind = rest[0]
            name = _flag(rest, "-n") or kind
            current = scene.add(kind, name, _flag(rest, "-p"))
        elif head == "setAttr" and rest and current is not None:
            names = [w for w in rest if w.startswith(".") and not _is_number(w)]
            target = current
            if not names:
                # `setAttr "node.attr" …` names its node itself.
                named = [w for w in rest if "." in w and not _is_number(w)]
                if not named:
                    continue
                node_name, _, attr = named[0].partition(".")
                target = scene.node(node_name)
                if target is None:
                    continue
                names = ["." + attr]
                rest = [w if w != named[0] else names[0] for w in rest]
            index = rest.index(names[0])
            _set(target, names[0], rest[:index] + rest[index + 1:])
        elif head == "select" and rest:
            name = [w for w in rest if not w.startswith("-")]
            if name:
                current = scene.node(name[-1].lstrip(":")) or scene.node(name[-1])
                if current is None:
                    current = scene.add("unknown", name[-1].lstrip(":"), None)
        elif head == "connectAttr":
            plain = [w for w in rest if not w.startswith("-")]
            if len(plain) >= 2:
                scene.connect(plain[0].lstrip(":"), plain[1].lstrip(":"))
        elif head == "parent" and "-add" in rest:
            plain = [w for w in rest if not w.startswith("-")]
            if len(plain) >= 2:
                shape = scene.node(plain[0])
                if shape is not None:
                    shape.extra_parents.append(plain[1].rsplit("|", 1)[-1])
        elif head == "currentUnit":
            scene.unit = _flag(rest, "-l") or _flag(rest, "-linear") or scene.unit
            scene.rate = _flag(rest, "-t") or _flag(rest, "-time") or scene.rate
        elif head == "requires" and len(rest) >= 2 and rest[0] == "maya":
            scene.version = rest[1]
        elif head == "fileInfo" and len(rest) >= 2 and rest[0] == "product":
            scene.creator = rest[1]
        elif head == "file" and "-r" in rest:
            paths = [w for w in rest if not w.startswith("-") and ("/" in w or "." in w)]
            if paths:
                scene.references.append(paths[-1])
        elif head == "upAxis" and "-ax" in rest:
            scene.up = (_flag(rest, "-ax") or "y").upper()
    scene.settle()
    return scene


def _flag(words: List[str], flag: str) -> Optional[str]:
    try:
        at = words.index(flag)
    except ValueError:
        return None
    return words[at + 1] if at + 1 < len(words) else None


# -- placement -------------------------------------------------------------------

def local_matrix(node: Node) -> List[float]:
    t = node.vector(".t")
    r = node.vector(".r")
    s = node.vector(".s", (1.0, 1.0, 1.0))
    rp = node.vector(".rp")
    sp = node.vector(".sp")
    ra = node.vector(".ra")
    order = _ROTATE_ORDER.get(int(node.number(".ro", 0)), 0)
    m = geometry.translation(-sp[0], -sp[1], -sp[2])
    m = geometry.multiply(m, geometry.scaling(*s))
    m = geometry.multiply(m, geometry.translation(*sp))
    m = geometry.multiply(m, geometry.translation(-rp[0], -rp[1], -rp[2]))
    m = geometry.multiply(m, geometry.rotation(ra, 0))
    m = geometry.multiply(m, geometry.rotation(r, order))
    if node.type == "joint":
        m = geometry.multiply(m, geometry.rotation(node.vector(".jo"), 0))
    m = geometry.multiply(m, geometry.translation(*rp))
    return geometry.multiply(m, geometry.translation(*t))


def world_matrix(scene: Scene, name: Optional[str], seen=None) -> List[float]:
    matrix = list(IDENTITY)
    seen = seen or set()
    while name and name not in seen:
        seen.add(name)
        node = scene.node(name)
        if node is None:
            break
        if node.type not in ("mesh",):
            matrix = geometry.multiply(matrix, local_matrix(node))
        name = node.parent
    return matrix


def placements(scene: Scene, name: Optional[str], depth: int = 0) -> List[List[float]]:
    """Every place [name] stands in the scene: one per path from the root.

    A transform or a shape instanced under a second parent (`parent -add`)
    is drawn once under each, and a child of an instanced transform is drawn
    under every one of its parent's places too."""
    if not name or depth > 64:
        return [list(IDENTITY)]
    node = scene.node(name)
    if node is None:
        return [list(IDENTITY)]
    if not _shown(node):
        return []
    local = local_matrix(node) if node.type != "mesh" else list(IDENTITY)
    out = []
    for parent in [node.parent] + node.extra_parents:
        for above in placements(scene, parent, depth + 1) if parent else [list(IDENTITY)]:
            out.append(geometry.multiply(local, above))
    return out


def _shown(node: Node) -> bool:
    found = node.values(".v")
    return not found or _float(found[0], 1.0) != 0.0


def visible(scene: Scene, name: Optional[str]) -> bool:
    seen = set()
    while name and name not in seen:
        seen.add(name)
        node = scene.node(name)
        if node is None:
            return True
        found = node.values(".v")
        if found and str(found[0]).lower() in ("no", "false", "0", "off"):
            return False
        name = node.parent
    return True


# -- a mesh's polygons ------------------------------------------------------------

class Polygons:
    """Points, faces as rings of point indices, and each corner's picture
    coordinates — whichever way they were come by."""

    def __init__(self):
        self.points: List[Tuple[float, float, float]] = []
        self.faces: List[List[int]] = []
        self.uvs: List[Optional[List[Tuple[float, float]]]] = []
        #: A face whose corners are shaded as one flat face.
        self.flat: List[bool] = []

    @property
    def triangles(self) -> int:
        return sum(max(0, len(f) - 2) for f in self.faces)


def _floats(entries: List[Tuple[int, list]], width: int) -> Dict[int, tuple]:
    out: Dict[int, tuple] = {}
    for start, values in entries:
        numbers = [_float(v) for v in values]
        for k in range(len(numbers) // width):
            out[start + k] = tuple(numbers[k * width:(k + 1) * width])
    return out


def stored(node: Node) -> Optional[Polygons]:
    """The polygons a mesh node holds itself, or None when it holds none."""
    points = _floats(node.indexed("vt"), 3)
    faces_raw = node.indexed("fc")
    if not points or not faces_raw:
        return None
    tweaks = _floats(node.indexed("pt"), 3)
    count = max(points) + 1
    polys = Polygons()
    for i in range(count):
        x, y, z = points.get(i, (0.0, 0.0, 0.0))
        dx, dy, dz = tweaks.get(i, (0.0, 0.0, 0.0))
        polys.points.append((x + dx, y + dy, z + dz))

    edges: Dict[int, Tuple[int, int, bool]] = {}
    for start, values in node.indexed("ed"):
        numbers = [int(_float(v)) for v in values]
        for k in range(len(numbers) // 3):
            a, b, smooth = numbers[k * 3:k * 3 + 3]
            edges[start + k] = (a, b, bool(smooth))

    uv_points: Dict[int, tuple] = {}
    for attr, entries in node.attrs.items():
        if attr.startswith("uvst[0].uvsp") and not attr.endswith("#type"):
            uv_points.update(_floats([(s or 0, v) for s, v in entries], 2))

    for _, values in faces_raw:
        i = 0
        face: Optional[List[int]] = None
        hard = False
        corner_uvs: Optional[List[Tuple[float, float]]] = None
        while i < len(values):
            word = values[i]
            if word == "f":
                if face is not None:
                    _add(polys, face, corner_uvs, hard)
                n = int(values[i + 1])
                ring = [int(v) for v in values[i + 2:i + 2 + n]]
                face, hard = [], True
                for e in ring:
                    edge = edges.get(e if e >= 0 else -e - 1)
                    if edge is None:
                        face = None
                        break
                    face.append(edge[0] if e >= 0 else edge[1])
                    hard = hard and not edge[2]
                corner_uvs = None
                i += 2 + n
            elif word in ("mu", "mf"):
                uvset = int(values[i + 1])
                n = int(values[i + 2])
                ids = [int(v) for v in values[i + 3:i + 3 + n]]
                if uvset == 0 and face is not None and n == len(face):
                    corner_uvs = [uv_points.get(k, (0.0, 0.0)) for k in ids]
                i += 3 + n
            elif word == "h":
                # A hole in the face: the face is kept, the hole is not cut.
                n = int(values[i + 1])
                i += 2 + n
            elif word in ("mc", "fc"):
                n = int(values[i + 2]) if i + 2 < len(values) else 0
                i += 3 + n
            else:
                i += 1
        if face is not None:
            _add(polys, face, corner_uvs, hard)
    return polys


def _add(polys: Polygons, face: List[int], uvs, flat: bool) -> None:
    if len(face) >= 3:
        polys.faces.append(face)
        polys.uvs.append(uvs)
        polys.flat.append(flat)


# -- primitives, built as Maya builds them --------------------------------------------

def _axis(node: Node) -> List[float]:
    """The rotation that stands a primitive on its axis (Y is its own)."""
    ax, ay, az = node.vector(".ax", (0.0, 1.0, 0.0))
    length = math.sqrt(ax * ax + ay * ay + az * az) or 1.0
    ax, ay, az = ax / length, ay / length, az / length
    # The rotation taking +Y to the axis.
    dot = ay
    if dot > 0.999999:
        return list(IDENTITY)
    if dot < -0.999999:
        return geometry.rotation((180.0, 0.0, 0.0), 0)
    kx, kz = az, -ax
    k = math.sqrt(kx * kx + kz * kz)
    kx, kz = kx / k, kz / k
    angle = math.acos(dot)
    c, s = math.cos(angle), math.sin(angle)
    t = 1 - c
    # Row-vector form of the axis-angle matrix about (kx, 0, kz).
    return [t * kx * kx + c, s * kz, t * kx * kz, 0,
            -s * kz, c, s * kx, 0,
            t * kx * kz, -s * kx, t * kz * kz + c, 0,
            0, 0, 0, 1]


def primitive(node: Node) -> Optional[Polygons]:
    kind = node.type
    polys = Polygons()
    if kind == "polyCube":
        _cube(polys, node.number(".w", 1), node.number(".h", 1), node.number(".d", 1),
              max(1, int(node.number(".sw", 1))), max(1, int(node.number(".sh", 1))),
              max(1, int(node.number(".sd", 1))))
    elif kind == "polyPlane":
        _plane(polys, node.number(".w", 1), node.number(".h", 1),
               max(1, int(node.number(".sw", 10))), max(1, int(node.number(".sh", 10))))
    elif kind == "polySphere":
        _sphere(polys, node.number(".r", 1), max(3, int(node.number(".sa", 20))),
                max(2, int(node.number(".sh", 20))))
    elif kind == "polyTorus":
        _torus(polys, node.number(".r", 1), node.number(".sr", 0.5),
               max(3, int(node.number(".sa", 20))), max(3, int(node.number(".sh", 20))),
               node.number(".tw", 0))
    elif kind in ("polyCylinder", "polyCone"):
        cone = kind == "polyCone"
        _cylinder(polys, node.number(".r", 1), node.number(".h", 2),
                  max(3, int(node.number(".sa", 20))), max(1, int(node.number(".sh", 1))),
                  cone)
    else:
        return None
    _collapse(polys)
    turn = _axis(node)
    if turn != IDENTITY:
        polys.points = [geometry.transform_point(turn, *p) for p in polys.points]
    return polys


def _collapse(polys: Polygons) -> None:
    """Corners standing on the same point taken out of a face: the rows at a
    sphere's poles and a cone's tip are triangles in Maya, not quads with a
    side of no length."""
    for index, face in enumerate(polys.faces):
        uvs = polys.uvs[index]
        keep = [k for k in range(len(face))
                if max(abs(a - b) for a, b in zip(polys.points[face[k]],
                                                  polys.points[face[k - 1]])) > 1e-9]
        if len(keep) != len(face):
            polys.faces[index] = [face[k] for k in keep]
            if uvs:
                polys.uvs[index] = [uvs[k] for k in keep]
    alive = [k for k, f in enumerate(polys.faces) if len(f) >= 3]
    polys.faces = [polys.faces[k] for k in alive]
    polys.uvs = [polys.uvs[k] for k in alive]
    polys.flat = [polys.flat[k] for k in alive]


def _grid(polys: Polygons, corner, u_axis, v_axis, nu: int, nv: int, flat: bool) -> None:
    base = len(polys.points)
    for j in range(nv + 1):
        for i in range(nu + 1):
            polys.points.append(tuple(corner[k] + u_axis[k] * i / nu + v_axis[k] * j / nv
                                      for k in range(3)))
    for j in range(nv):
        for i in range(nu):
            a = base + j * (nu + 1) + i
            face = [a, a + 1, a + nu + 2, a + nu + 1]
            polys.faces.append(face)
            polys.uvs.append([(i / nu, 1 - j / nv), ((i + 1) / nu, 1 - j / nv),
                              ((i + 1) / nu, 1 - (j + 1) / nv), (i / nu, 1 - (j + 1) / nv)])
            polys.flat.append(flat)


def _cube(polys, w, h, d, sw, sh, sd):
    x, y, z = w / 2, h / 2, d / 2
    # Six grids, each wound so that its face looks outwards.
    _grid(polys, (-x, -y, z), (w, 0, 0), (0, h, 0), sw, sh, True)      # front
    _grid(polys, (x, -y, -z), (-w, 0, 0), (0, h, 0), sw, sh, True)     # back
    _grid(polys, (-x, y, z), (w, 0, 0), (0, 0, -d), sw, sd, True)      # top
    _grid(polys, (-x, -y, -z), (w, 0, 0), (0, 0, d), sw, sd, True)     # bottom
    _grid(polys, (x, -y, z), (0, 0, -d), (0, h, 0), sd, sh, True)      # right
    _grid(polys, (-x, -y, -z), (0, 0, d), (0, h, 0), sd, sh, True)     # left


def _plane(polys, w, h, sw, sh):
    _grid(polys, (-w / 2, 0, h / 2), (w, 0, 0), (0, 0, -h), sw, sh, True)


def _ring(polys, radius_at, y_at, n: int, rows: int, v_of):
    base = len(polys.points)
    for j in range(rows + 1):
        for i in range(n + 1):
            a = 2 * math.pi * i / n
            r = radius_at(j)
            polys.points.append((r * math.sin(a), y_at(j), r * math.cos(a)))
    for j in range(rows):
        for i in range(n):
            a = base + j * (n + 1) + i
            polys.faces.append([a, a + 1, a + n + 2, a + n + 1])
            polys.uvs.append([(i / n, v_of(j)), ((i + 1) / n, v_of(j)),
                              ((i + 1) / n, v_of(j + 1)), (i / n, v_of(j + 1))])
            polys.flat.append(False)


def _sphere(polys, r, sa, sh):
    _ring(polys, lambda j: r * math.sin(math.pi * j / sh),
          lambda j: -r * math.cos(math.pi * j / sh), sa, sh, lambda j: 1 - j / sh)


def _torus(polys, r, sr, sa, sh, twist):
    base = len(polys.points)
    for j in range(sh + 1):
        b = 2 * math.pi * j / sh + math.radians(twist)
        for i in range(sa + 1):
            a = 2 * math.pi * i / sa
            ring = r + sr * math.cos(b)
            polys.points.append((ring * math.sin(a), sr * math.sin(b), ring * math.cos(a)))
    for j in range(sh):
        for i in range(sa):
            a = base + j * (sa + 1) + i
            polys.faces.append([a, a + sa + 1, a + sa + 2, a + 1])
            polys.uvs.append([(i / sa, 1 - j / sh), (i / sa, 1 - (j + 1) / sh),
                              ((i + 1) / sa, 1 - (j + 1) / sh), ((i + 1) / sa, 1 - j / sh)])
            polys.flat.append(False)


def _cylinder(polys, r, h, sa, sh, cone):
    y0 = -h / 2
    radius = (lambda j: r * (1 - j / sh)) if cone else (lambda j: r)
    _ring(polys, radius, lambda j: y0 + h * j / sh, sa, sh, lambda j: 1 - j / sh)
    # The caps: one ring each, flat.
    for top in ((False, True) if not cone else (False,)):
        y = h / 2 if top else y0
        base = len(polys.points)
        for i in range(sa):
            a = 2 * math.pi * i / sa
            polys.points.append((r * math.sin(a), y, r * math.cos(a)))
        ring = list(range(base, base + sa))
        polys.faces.append(ring if top else list(reversed(ring)))
        polys.uvs.append(None)
        polys.flat.append(True)


# -- finding a mesh's polygons ---------------------------------------------------------

def polygons_of(scene: Scene, shape: Node, seen=None) -> Tuple[Optional[Polygons], str]:
    """The polygons of [shape] and, when there are none, the node in the way."""
    seen = seen or set()
    own = stored(shape)
    upstream = scene.into(shape.name)
    if own is not None and upstream is None:
        return own, ""
    name = shape.name
    tweaks: List[Node] = []
    while upstream is not None:
        src, _ = upstream
        if src in seen:
            break
        seen.add(src)
        node = scene.node(src)
        if node is None:
            break
        if node.type == "mesh":
            polys = stored(node)
            if polys is None:
                inner, blocker = polygons_of(scene, node, seen)
                polys = inner
                if polys is None:
                    return None, blocker
            return _tweaked(polys, tweaks), ""
        if node.type in _PRIMITIVES:
            return _tweaked(primitive(node), tweaks), ""
        if node.type in _PASSING:
            if node.type == "polyTweak":
                tweaks.append(node)
            upstream = scene.into(node.name)
            continue
        return (own, "") if own is not None else (None, node.type)
    return own, "" if own is not None else "no input"


def _tweaked(polys: Optional[Polygons], tweaks: List[Node]) -> Optional[Polygons]:
    if polys is None:
        return None
    for tweak in tweaks:
        for index, (dx, dy, dz) in _floats(tweak.indexed("tk"), 3).items():
            if 0 <= index < len(polys.points):
                x, y, z = polys.points[index]
                polys.points[index] = (x + dx, y + dy, z + dz)
    return polys


# -- surfaces ---------------------------------------------------------------------------

_COLOUR_ATTRS = (".c", ".bc", ".base_color", ".baseColor", ".color", ".dc")


def colour_of(scene: Scene, shape: Node) -> str:
    """The colour of the material the shape is in, as #RRGGBB, or ''."""
    for target in scene.outgoing.get(shape.name, []):
        engine = scene.node(target.split(".", 1)[0])
        # The default one is not made by the file, only selected by it.
        if engine is None or (engine.type != "shadingEngine"
                              and not engine.name.endswith("initialShadingGroup")):
            continue
        found = scene.incoming.get(engine.name + ".ss")
        material = scene.node(found.split(".", 1)[0]) if found else None
        if material is None:
            continue
        for attr in _COLOUR_ATTRS:
            value = material.values(attr)
            if value and len(value) >= 3:
                rgb = [_float(v) for v in value[:3]]
                return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(c * 255)))) for c in rgb)
        return "#808080"
    return ""


# -- the answer -----------------------------------------------------------------------

def meshes(scene: Scene, max_triangles: int = 400000) -> Tuple[List[dict], dict]:
    out: List[dict] = []
    total = held = dropped = 0
    unbuilt: Dict[str, int] = {}
    for shape in list(scene.nodes.values()):
        if shape.type != "mesh" or shape.flag(".io"):
            continue
        polys, blocker = polygons_of(scene, shape)
        if polys is None or not polys.faces:
            if blocker and blocker != "no input":
                unbuilt[blocker] = unbuilt.get(blocker, 0) + 1
            continue
        held += polys.triangles
        places = []
        for parent in [shape.parent] + shape.extra_parents:
            places += [(parent, m) for m in placements(scene, parent)]
        for parent, matrix in places:
            if total + polys.triangles > max_triangles:
                dropped += 1
                continue
            part = _part(scene, shape, parent, polys, matrix)
            total += len(part["indices"]) // 3
            out.append(part)
    return out, {"droppedMeshes": dropped, "triangles": total, "held": max(total, held),
                 "unbuilt": unbuilt}


def _part(scene: Scene, shape: Node, parent: Optional[str], polys: Polygons,
          matrix: List[float]) -> dict:
    points = [geometry.transform_point(matrix, *p) for p in polys.points]
    face_normals = []
    for face in polys.faces:
        nx = ny = nz = 0.0
        for k in range(len(face)):
            ax, ay, az = points[face[k]]
            bx, by, bz = points[face[(k + 1) % len(face)]]
            nx += (ay - by) * (az + bz)
            ny += (az - bz) * (ax + bx)
            nz += (ax - bx) * (ay + by)
        face_normals.append(geometry._normalise(nx, ny, nz))
    smooth = [[0.0, 0.0, 0.0] for _ in points]
    for face, flat, normal in zip(polys.faces, polys.flat, face_normals):
        if flat:
            continue
        for v in face:
            s = smooth[v]
            s[0] += normal[0]
            s[1] += normal[1]
            s[2] += normal[2]
    builder = MeshBuilder()
    for face, uvs, flat, normal in zip(polys.faces, polys.uvs, polys.flat, face_normals):
        corners = []
        for k, v in enumerate(face):
            n = normal if flat else geometry._normalise(*smooth[v])
            uv = uvs[k] if uvs else (0.0, 0.0)
            # Maya's picture coordinates run up from the bottom.
            corners.append(builder.corner(points[v], n, (uv[0], 1.0 - uv[1]), v))
        for k in range(1, len(corners) - 1):
            builder.triangle(corners[0], corners[k], corners[k + 1])
    name = (parent or shape.name).rsplit("|", 1)[-1]
    return {
        "name": name,
        "positions": builder.positions,
        "normals": builder.normals,
        "uvs": builder.uvs,
        "indices": builder.indices,
        "sources": builder.sources,
        "sourceCount": len(points),
        "geometryId": id(shape) & 0x7FFFFFFF,
        "modelId": None,
        "placement_no_fix": list(IDENTITY),
        "color": colour_of(scene, shape),
        "picture": None,
    }


def summarise(scene: Scene, parts: List[dict], note: dict) -> dict:
    kinds: Dict[str, int] = {}
    for node in scene.nodes.values():
        if node.type in ("transform", "mesh", "joint", "camera", "nurbsCurve",
                         "nurbsSurface", "locator", "directionalLight", "pointLight",
                         "spotLight", "areaLight", "ambientLight"):
            kinds[node.type] = kinds.get(node.type, 0) + 1
    curves = sum(1 for n in scene.nodes.values() if n.type.startswith("animCurve"))
    rate = _RATES.get(scene.rate)
    if rate is None:
        found = re.match(r"([\d.]+)fps", scene.rate or "")
        rate = float(found.group(1)) if found else 24.0
    return {
        "version": 0,
        "binary": scene.binary,
        "creator": scene.creator or "Maya",
        "mayaVersion": scene.version,
        "unitScale": _UNITS.get(scene.unit, 1.0),
        "upAxis": scene.up,
        "frameRate": rate,
        "objects": len(scene.nodes),
        "connections": len(scene.incoming),
        "models": kinds,
        "meshes": [{"name": mesh["name"],
                    "vertices": len(mesh["positions"]) // 3,
                    "polygons": len(mesh["indices"]) // 3,
                    "triangles": len(mesh["indices"]) // 3}
                   for mesh in parts],
        "materials": sum(1 for n in scene.nodes.values() if n.type == "shadingEngine"),
        "textures": sum(1 for n in scene.nodes.values() if n.type == "file"),
        "embedded": 0,
        "embeddedBytes": 0,
        "skins": sum(1 for n in scene.nodes.values() if n.type == "skinCluster"),
        "clusters": 0,
        "joints": kinds.get("joint", 0),
        "clips": [],
        "animCurves": curves,
        "references": list(scene.references),
        "unbuilt": note.get("unbuilt", {}),
    }


def read(data: bytes) -> Scene:
    if data[:4] in (b"FOR4", b"FOR8"):
        import mayabinary
        return mayabinary.parse(data)
    head = data[:64].lstrip()
    if not head.startswith(b"//Maya"):
        raise MayaError("This is not a Maya scene.")
    return parse(data.decode("utf-8", "replace"))
