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

"""COLLADA — `.dae`, and `.zae`, the same in a zip — read without a library.

**A document, and a scene in it.** Geometries, materials, effects and images
are libraries of things with ids; the visual scene is a tree of nodes that
places them, each node's transform written as a list of steps — a matrix, a
translate, a rotate about an axis, a scale — and each instance of a geometry
binding its material symbols to materials. So a mesh is found by walking the
scene, not by reading the geometries: a geometry placed twice is drawn twice,
and one never placed is not drawn (unless nothing is placed at all).

**Matrices are written for column vectors**, the other way round from the
row vectors every matrix here meets; each is transposed as it is read, and a
node's steps are multiplied in the order that makes that right.

**A primitive** — `triangles`, `polylist`, `polygons`, `tristrips`,
`trifans` — lists, per corner, one index for each of its inputs at their
offsets: the vertex, the normal, the texture coordinate of a set. Picture
coordinates run up from the bottom and are turned for the view.

**A material** is an effect; its `diffuse` is a colour or a texture, the
texture a sampler naming a surface naming an image naming a file.
"""

from __future__ import annotations

import io
import math
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

import geometry
from geometry import IDENTITY, MeshBuilder


class ColladaError(Exception):
    pass


def _strip(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _floats(text: Optional[str]) -> List[float]:
    if not text:
        return []
    out = []
    for word in text.split():
        try:
            out.append(float(word))
        except ValueError:
            # A decimal comma, from an exporter that wrote in its locale.
            try:
                out.append(float(word.replace(",", ".")))
            except ValueError:
                out.append(0.0)
    return out


def _ints(text: Optional[str]) -> List[int]:
    if not text:
        return []
    out = []
    for word in text.split():
        try:
            out.append(int(word))
        except ValueError:
            pass
    return out


def _transpose(m: List[float]) -> List[float]:
    return [m[c * 4 + r] for r in range(4) for c in range(4)]


def _rotation(x: float, y: float, z: float, degrees: float) -> List[float]:
    """Rotation about an axis, for row vectors."""
    n = math.sqrt(x * x + y * y + z * z) or 1.0
    x, y, z = x / n, y / n, z / n
    a = math.radians(degrees)
    c, s, t = math.cos(a), math.sin(a), 1 - math.cos(a)
    column = [t * x * x + c, t * x * y - s * z, t * x * z + s * y, 0,
              t * x * y + s * z, t * y * y + c, t * y * z - s * x, 0,
              t * x * z - s * y, t * y * z + s * x, t * z * z + c, 0,
              0, 0, 0, 1]
    return _transpose(column)


def _node_matrix(node: ET.Element) -> List[float]:
    """A node's transform steps, as one matrix for row vectors.

    For column vectors the steps compose left to right, `T1·T2·…·Tn`; met by a
    row vector the same transform is the transposes in the other order.
    """
    out = list(IDENTITY)
    for step in node:
        kind = _strip(step.tag)
        values = _floats(step.text)
        if kind == "matrix" and len(values) == 16:
            m = _transpose(values)
        elif kind == "translate" and len(values) >= 3:
            m = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, values[0], values[1], values[2], 1]
        elif kind == "rotate" and len(values) >= 4:
            m = _rotation(*values[:4])
        elif kind == "scale" and len(values) >= 3:
            m = [values[0], 0, 0, 0, 0, values[1], 0, 0, 0, 0, values[2], 0, 0, 0, 0, 1]
        elif kind == "lookat" and len(values) >= 9:
            m = _look_at(values)
        else:
            continue
        out = geometry.multiply(m, out)
    return out


def _look_at(v: List[float]) -> List[float]:
    eye, target, up = v[0:3], v[3:6], v[6:9]
    f = geometry._normalise(*(target[i] - eye[i] for i in range(3)))
    s = geometry._normalise(f[1] * up[2] - f[2] * up[1], f[2] * up[0] - f[0] * up[2],
                            f[0] * up[1] - f[1] * up[0])
    u = (s[1] * f[2] - s[2] * f[1], s[2] * f[0] - s[0] * f[2], s[0] * f[1] - s[1] * f[0])
    return [s[0], s[1], s[2], 0, u[0], u[1], u[2], 0, -f[0], -f[1], -f[2], 0,
            eye[0], eye[1], eye[2], 1]


class Document:
    def __init__(self, data: bytes, url: str = ""):
        self.url = url
        self.zipped: Optional[zipfile.ZipFile] = None
        self.inner = ""
        if data[:2] == b"PK":
            self.zipped = zipfile.ZipFile(io.BytesIO(data))
            self.inner = self._zae_root()
            data = self.zipped.read(self.inner)
        try:
            self.root = ET.fromstring(data)
        except ET.ParseError as failure:
            raise ColladaError("This COLLADA file is not readable XML: %s" % failure)
        if _strip(self.root.tag) != "COLLADA":
            raise ColladaError("This is not a COLLADA file.")
        self.ns = self.root.tag[:self.root.tag.index("}") + 1] if self.root.tag.startswith("{") else ""
        self.ids: Dict[str, ET.Element] = {}
        for element in self.root.iter():
            ident = element.get("id")
            if ident:
                self.ids[ident] = element
        asset = self.find(self.root, "asset")
        up = self.find(asset, "up_axis") if asset is not None else None
        self.up = (up.text or "Y_UP").strip().upper() if up is not None else "Y_UP"
        unit = self.find(asset, "unit") if asset is not None else None
        try:
            self.meters = float(unit.get("meter", "1")) if unit is not None else 1.0
        except ValueError:
            self.meters = 1.0
        tool = self.root.find(".//%sauthoring_tool" % self.ns)
        self.tool = (tool.text or "").strip() if tool is not None else ""
        self._materials: Dict[str, Tuple[str, Optional[dict]]] = {}

    def _zae_root(self) -> str:
        names = self.zipped.namelist()
        if "manifest.xml" in names:
            try:
                manifest = ET.fromstring(self.zipped.read("manifest.xml"))
                found = manifest.find(".//dae_root")
                if found is not None and found.text:
                    return found.text.strip().lstrip("./")
            except ET.ParseError:
                pass
        for name in names:
            if name.lower().endswith(".dae"):
                return name
        raise ColladaError("This .zae holds no COLLADA document.")

    def find(self, element: Optional[ET.Element], name: str) -> Optional[ET.Element]:
        if element is None:
            return None
        return element.find(self.ns + name)

    def all(self, element: Optional[ET.Element], name: str) -> List[ET.Element]:
        if element is None:
            return []
        return element.findall(self.ns + name)

    def ref(self, url: Optional[str]) -> Optional[ET.Element]:
        if not url:
            return None
        return self.ids.get(url.lstrip("#"))

    # -- sources --

    def source(self, url: str) -> Tuple[List[float], int]:
        """A source's numbers and how many make one value."""
        element = self.ref(url)
        if element is None:
            return [], 1
        if _strip(element.tag) == "vertices":
            for put in self.all(element, "input"):
                if put.get("semantic") == "POSITION":
                    return self.source(put.get("source", ""))
            return [], 1
        array = self.find(element, "float_array")
        values = _floats(array.text if array is not None else "")
        accessor = element.find(".//%saccessor" % self.ns)
        stride = int(accessor.get("stride", "1")) if accessor is not None else 1
        return values, max(1, stride)

    # -- materials --

    def material(self, material_id: str, picture_place) -> Tuple[str, Optional[dict]]:
        """``(colour, picture)`` of a material: its effect's diffuse."""
        if material_id in self._materials:
            return self._materials[material_id]
        answer = ("", None)
        material = self.ids.get(material_id)
        effect = self.ref(self.find(material, "instance_effect").get("url")) \
            if material is not None and self.find(material, "instance_effect") is not None else None
        if effect is not None:
            diffuse = None
            for shading in ("phong", "lambert", "blinn", "constant"):
                found = effect.find(".//%s%s" % (self.ns, shading))
                if found is not None:
                    # Not `or`: an element with no children is false.
                    diffuse = self.find(found, "diffuse")
                    if diffuse is None:
                        diffuse = self.find(found, "emission")
                    break
            if diffuse is not None:
                colour = self.find(diffuse, "color")
                texture = self.find(diffuse, "texture")
                hexed = ""
                if colour is not None:
                    rgb = _floats(colour.text)[:3]
                    if len(rgb) == 3:
                        hexed = "#%02X%02X%02X" % tuple(
                            max(0, min(255, int(round(c * 255)))) for c in rgb)
                picture = None
                if texture is not None:
                    path = self._image_path(effect, texture.get("texture", ""))
                    if path:
                        picture = picture_place(path)
                answer = (hexed, picture)
        self._materials[material_id] = answer
        return answer

    def _image_path(self, effect: ET.Element, sampler: str) -> str:
        """sampler → surface → image → file, or the image named directly."""
        image = self.ids.get(sampler)
        params = {p.get("sid"): p for p in effect.iter(self.ns + "newparam")}
        param = params.get(sampler)
        if param is not None:
            sampled = param.find(".//%ssource" % self.ns)
            if sampled is not None and sampled.text:
                surface = params.get(sampled.text.strip())
                init = surface.find(".//%sinit_from" % self.ns) if surface is not None else None
                if init is not None and init.text:
                    image = self.ids.get(init.text.strip())
            else:
                inst = param.find(".//%sinstance_image" % self.ns)
                if inst is not None:
                    image = self.ref(inst.get("url"))
        if image is None or _strip(image.tag) != "image":
            return ""
        init = image.find(".//%sinit_from" % self.ns)
        if init is None:
            return ""
        ref = init.find(self.ns + "ref")
        text = (ref.text if ref is not None else init.text) or ""
        return text.strip()


def _picture_place(doc: Document, path: str) -> dict:
    """Where a picture is: inside the .zae, or a path beside the file."""
    path = re.sub(r"^file:/*", "/" if path.startswith("file:///") else "", path)
    from urllib.parse import unquote
    path = unquote(path).replace("\\", "/")
    name = path.rsplit("/", 1)[-1]
    if doc.zipped is not None:
        folder = posixpath.dirname(doc.inner)
        for candidate in (posixpath.normpath(posixpath.join(folder, path)).lstrip("./"),
                          path.lstrip("./"), name):
            try:
                return {"bytes": doc.zipped.read(candidate), "name": name}
            except KeyError:
                continue
    rooted = path.startswith("/") or (len(path) > 1 and path[1] == ":")
    return {"name": name, "beside": None if rooted else path}


# -- the scene ------------------------------------------------------------------------

def _primitives(doc: Document, geometry_element: ET.Element):
    mesh = doc.find(geometry_element, "mesh")
    if mesh is None:
        return
    for element in mesh:
        kind = _strip(element.tag)
        if kind in ("triangles", "polylist", "polygons", "tristrips", "trifans", "lines",
                    "linestrips"):
            yield kind, element


def _faces(doc: Document, kind: str, element: ET.Element) -> List[List[List[int]]]:
    """Each face as its corners, each corner the indices at every offset."""
    inputs = doc.all(element, "input")
    width = max([int(p.get("offset", "0")) for p in inputs] + [0]) + 1
    faces: List[List[List[int]]] = []

    def corners(values: List[int]) -> List[List[int]]:
        return [values[i:i + width] for i in range(0, len(values) - width + 1, width)]

    if kind == "triangles":
        cs = corners(_ints(doc.find(element, "p").text if doc.find(element, "p") is not None else ""))
        faces = [cs[i:i + 3] for i in range(0, len(cs) - 2, 3)]
    elif kind == "polylist":
        counts = _ints(doc.find(element, "vcount").text if doc.find(element, "vcount") is not None else "")
        cs = corners(_ints(doc.find(element, "p").text if doc.find(element, "p") is not None else ""))
        at = 0
        for n in counts:
            faces.append(cs[at:at + n])
            at += n
    elif kind == "polygons":
        for p in doc.all(element, "p"):
            faces.append(corners(_ints(p.text)))
        for ph in doc.all(element, "ph"):
            p = doc.find(ph, "p")
            if p is not None:
                faces.append(corners(_ints(p.text)))
    elif kind in ("tristrips", "trifans"):
        for p in doc.all(element, "p"):
            cs = corners(_ints(p.text))
            for i in range(len(cs) - 2):
                if kind == "trifans":
                    faces.append([cs[0], cs[i + 1], cs[i + 2]])
                elif i % 2 == 0:
                    faces.append([cs[i], cs[i + 1], cs[i + 2]])
                else:
                    faces.append([cs[i + 1], cs[i], cs[i + 2]])
    return [f for f in faces if len(f) >= 3]


class Scene:
    def __init__(self, doc: Document):
        self.doc = doc
        #: (geometry element, world matrix, {symbol: material id}, name)
        self.placed: List[tuple] = []
        self.counts: Dict[str, int] = {}
        self.animations = len(doc.root.findall(".//%sanimation" % doc.ns))
        scene = doc.find(doc.root, "scene")
        visual = doc.ref(doc.find(scene, "instance_visual_scene").get("url")) \
            if scene is not None and doc.find(scene, "instance_visual_scene") is not None else None
        if visual is None:
            scenes = doc.root.findall(".//%svisual_scene" % doc.ns)
            visual = scenes[0] if scenes else None
        fix = list(IDENTITY)
        if doc.up == "Z_UP":
            fix = [1.0, 0, 0, 0, 0, 0, -1.0, 0, 0, 1.0, 0, 0, 0, 0, 0, 1.0]
        elif doc.up == "X_UP":
            fix = [0, 1.0, 0, 0, -1.0, 0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
        if visual is not None:
            for node in doc.all(visual, "node"):
                self._walk(node, fix, 0)
        if not self.placed:
            for element in doc.root.findall(".//%sgeometry" % doc.ns):
                self.placed.append((element, fix, {}, element.get("name") or element.get("id", "")))

    def _walk(self, node: ET.Element, parent: List[float], depth: int) -> None:
        if depth > 64:
            return
        doc = self.doc
        world = geometry.multiply(_node_matrix(node), parent)
        kind = node.get("type", "NODE")
        self.counts[kind] = self.counts.get(kind, 0) + 1
        name = node.get("name") or node.get("id") or ""
        for inst in doc.all(node, "instance_geometry"):
            geometry_element = doc.ref(inst.get("url"))
            if geometry_element is not None:
                self.placed.append((geometry_element, world, self._bindings(inst), name))
        for inst in doc.all(node, "instance_controller"):
            controller = doc.ref(inst.get("url"))
            skin = doc.find(controller, "skin") if controller is not None else None
            if skin is None:
                continue
            geometry_element = doc.ref(skin.get("source"))
            bind = doc.find(skin, "bind_shape_matrix")
            shape = _transpose(_floats(bind.text)) if bind is not None and \
                len(_floats(bind.text)) == 16 else list(IDENTITY)
            if geometry_element is not None:
                self.counts["skin"] = self.counts.get("skin", 0) + 1
                self.placed.append((geometry_element, geometry.multiply(shape, world),
                                    self._bindings(inst), name))
        for inst in doc.all(node, "instance_node"):
            target = doc.ref(inst.get("url"))
            if target is not None:
                self._walk(target, world, depth + 1)
        for child in doc.all(node, "node"):
            self._walk(child, world, depth + 1)

    def _bindings(self, instance: ET.Element) -> Dict[str, str]:
        out = {}
        for bound in instance.iter(self.doc.ns + "instance_material"):
            out[bound.get("symbol", "")] = bound.get("target", "").lstrip("#")
        return out


def read(data: bytes, url: str = "") -> Scene:
    return Scene(Document(data, url))


def meshes(scene: Scene, max_triangles: int = 400000) -> Tuple[List[dict], dict]:
    doc = scene.doc
    out: List[dict] = []
    total = dropped = 0
    other: Dict[str, int] = {}
    place = lambda path: _picture_place(doc, path)  # noqa: E731
    for slot, (element, world, bindings, name) in enumerate(scene.placed):
        flip = _det(world) < 0
        for kind, prim in _primitives(doc, element):
            if kind in ("lines", "linestrips"):
                other["lines"] = other.get("lines", 0) + 1
                continue
            inputs = doc.all(prim, "input")
            offsets = {}
            for put in inputs:
                semantic = put.get("semantic")
                key = semantic if semantic != "TEXCOORD" else "TEXCOORD%s" % put.get("set", "0")
                if key not in offsets:
                    offsets[key] = (int(put.get("offset", "0")), put.get("source", ""))
            if "VERTEX" not in offsets:
                continue
            positions, pstride = doc.source(offsets["VERTEX"][1])
            vertex_element = doc.ref(offsets["VERTEX"][1])
            normals, nstride = ([], 3)
            if "NORMAL" in offsets:
                normals, nstride = doc.source(offsets["NORMAL"][1])
            elif vertex_element is not None:
                for put in doc.all(vertex_element, "input"):
                    if put.get("semantic") == "NORMAL":
                        normals, nstride = doc.source(put.get("source", ""))
                        offsets["NORMAL"] = (offsets["VERTEX"][0], "")
            uv_key = next((k for k in sorted(offsets) if k.startswith("TEXCOORD")), None)
            uvs, ustride = doc.source(offsets[uv_key][1]) if uv_key else ([], 2)
            faces = _faces(doc, kind, prim)
            material_id = bindings.get(prim.get("material", ""), prim.get("material", ""))
            colour, picture = doc.material(material_id, place) if material_id else ("", None)
            builder = MeshBuilder()
            count = len(positions) // pstride
            for face in faces:
                if total + len(face) - 2 > max_triangles:
                    dropped += 1
                    continue
                corners = []
                points = []
                for corner in face:
                    v = corner[offsets["VERTEX"][0]] if offsets["VERTEX"][0] < len(corner) else -1
                    if not 0 <= v < count:
                        corners = []
                        break
                    p = geometry.transform_point(world, *positions[v * pstride:v * pstride + 3])
                    n = None
                    if "NORMAL" in offsets and normals:
                        i = corner[offsets["NORMAL"][0]]
                        if 0 <= i * nstride + 2 < len(normals):
                            n = geometry._normalise(*geometry.transform_direction(
                                world, *normals[i * nstride:i * nstride + 3]))
                            if flip:
                                n = (-n[0], -n[1], -n[2])
                    uv = (0.0, 0.0)
                    if uv_key and uvs:
                        i = corner[offsets[uv_key][0]]
                        if 0 <= i * ustride + 1 < len(uvs):
                            uv = (uvs[i * ustride], 1.0 - uvs[i * ustride + 1])
                    points.append(p)
                    corners.append((p, n, uv, v))
                if len(corners) < 3:
                    continue
                if any(c[1] is None for c in corners):
                    a, b, c = points[0], points[1], points[2]
                    u = [b[i] - a[i] for i in range(3)]
                    w = [c[i] - a[i] for i in range(3)]
                    face_n = geometry._normalise(u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2],
                                                 u[0] * w[1] - u[1] * w[0])
                    if flip:
                        face_n = (-face_n[0], -face_n[1], -face_n[2])
                    corners = [(p, n if n is not None else face_n, uv, v) for p, n, uv, v in corners]
                made = [builder.corner(p, n, uv, v) for p, n, uv, v in corners]
                for i in range(1, len(made) - 1):
                    if flip:
                        builder.triangle(made[0], made[i + 1], made[i])
                    else:
                        builder.triangle(made[0], made[i], made[i + 1])
                total += len(face) - 2
            if not builder.indices:
                continue
            out.append({
                "name": name or element.get("name") or element.get("id", "Mesh"),
                "positions": builder.positions,
                "normals": builder.normals,
                "uvs": builder.uvs,
                "indices": builder.indices,
                "sources": builder.sources,
                "sourceCount": count,
                "geometryId": slot,
                "modelId": None,
                "placement_no_fix": list(IDENTITY),
                "color": colour,
                "picture": dict(picture) if picture else None,
            })
    return out, {"droppedMeshes": dropped, "triangles": total, "held": total, "other": other}


def _det(m: List[float]) -> float:
    return (m[0] * (m[5] * m[10] - m[6] * m[9]) - m[1] * (m[4] * m[10] - m[6] * m[8])
            + m[2] * (m[4] * m[9] - m[5] * m[8]))


def summarise(scene: Scene, parts: List[dict], note: dict) -> dict:
    doc = scene.doc
    return {
        "version": 0,
        "binary": doc.zipped is not None,
        "creator": doc.tool or "COLLADA",
        "unitScale": doc.meters * 100.0,
        "upAxis": {"Z_UP": "Z", "X_UP": "X"}.get(doc.up, "Y"),
        "frameRate": 30.0,
        "objects": sum(scene.counts.values()),
        "connections": 0,
        "models": dict(scene.counts),
        "meshes": [{"name": mesh["name"],
                    "vertices": len(mesh["positions"]) // 3,
                    "polygons": len(mesh["indices"]) // 3,
                    "triangles": len(mesh["indices"]) // 3}
                   for mesh in parts],
        "materials": len(doc.root.findall(".//%smaterial" % doc.ns)),
        "textures": len(doc.root.findall(".//%simage" % doc.ns)),
        "embedded": 0,
        "embeddedBytes": 0,
        "skins": scene.counts.get("skin", 0),
        "clusters": 0,
        "joints": scene.counts.get("JOINT", 0),
        "clips": [],
        "animCurves": scene.animations,
        "other": note.get("other", {}),
    }
