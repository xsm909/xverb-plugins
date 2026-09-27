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

"""The plain mesh formats: STL, PLY, OFF and 3MF.

Each is a list of points and a list of faces and little else, so each
reader only has to say what its file holds — points, faces, and whatever
colour, normal or picture coordinate it carries — and `_parts` builds the
meshes the same way for all of them.

**Colour.** The view paints a mesh one colour, so faces are gathered by
colour: an STL's per-triangle colours, a PLY's per-vertex ones, a 3MF's
materials each become a mesh of their own. A file with thousands of shades
is brought down to a few dozen by coarser rounding until it fits.

**Which way is up.** 3MF is Z-up by its specification and STL by the habit of
every 3D printer; both are tipped back a quarter turn so the view, which is
Y-up, stands them up. PLY and OFF say nothing and are left as they are.
"""

from __future__ import annotations

import io
import math
import re
import struct
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Sequence, Tuple

import geometry
from geometry import IDENTITY, MeshBuilder


class MeshFileError(Exception):
    pass


Point = Tuple[float, float, float]


class Mesh:
    """What a file said: points, faces, and what each face or point carries."""

    def __init__(self, name: str = ""):
        self.name = name
        self.points: List[Point] = []
        self.faces: List[List[int]] = []
        #: Per face: a colour key ("#RRGGBB" or ""); per point: (r, g, b) floats.
        self.face_colours: List[str] = []
        self.point_colours: List[Tuple[float, float, float]] = []
        self.point_normals: List[Point] = []
        self.point_uvs: List[Tuple[float, float]] = []
        #: Per face, per corner: (u, v) — 3MF's texture coordinates.
        self.corner_uvs: List[Optional[List[Tuple[float, float]]]] = []
        self.face_pictures: List[Optional[str]] = []
        self.flat = False
        self.points_only = 0


# -- building ------------------------------------------------------------------------

_Z_UP = [1.0, 0, 0, 0, 0, 0, -1.0, 0, 0, 1.0, 0, 0, 0, 0, 0, 1.0]


def _hex(r: float, g: float, b: float) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(c * 255)))) for c in (r, g, b))


def _face_keys(mesh: Mesh) -> List[str]:
    """A colour key per face, brought down to a few dozen distinct ones."""
    count = len(mesh.faces)
    if mesh.face_colours and any(mesh.face_colours):
        keys = list(mesh.face_colours) + [""] * (count - len(mesh.face_colours))
    elif mesh.point_colours and len(mesh.point_colours) == len(mesh.points):
        averaged = []
        for face in mesh.faces:
            cs = [mesh.point_colours[i] for i in face if i < len(mesh.point_colours)]
            n = len(cs) or 1
            averaged.append((sum(c[0] for c in cs) / n, sum(c[1] for c in cs) / n,
                             sum(c[2] for c in cs) / n))
        for levels in (32, 16, 8, 4, 2):
            keys = [_hex(*(round(c * (levels - 1)) / (levels - 1) for c in rgb)) for rgb in averaged]
            if len(set(keys)) <= 48:
                break
        return keys
    else:
        return [""] * count
    distinct = set(keys)
    if len(distinct) > 48:
        for levels in (16, 8, 4, 2):
            rounded = []
            for k in keys:
                if not k:
                    rounded.append("")
                    continue
                rgb = [int(k[i:i + 2], 16) / 255 for i in (1, 3, 5)]
                rounded.append(_hex(*(round(c * (levels - 1)) / (levels - 1) for c in rgb)))
            keys = rounded
            if len(set(keys)) <= 48:
                break
    return keys


def _parts(mesh: Mesh, z_up: bool, pictures: Optional[Dict[str, dict]] = None,
           budget: int = 400000) -> Tuple[List[dict], int, int]:
    """The mesh, cut by colour and picture, as the view's parts."""
    fix = _Z_UP if z_up else IDENTITY
    points = [geometry.transform_point(fix, *p) for p in mesh.points]
    normals_given = len(mesh.point_normals) == len(mesh.points) and not mesh.flat
    given = [geometry._normalise(*geometry.transform_direction(fix, *n))
             for n in mesh.point_normals] if normals_given else []

    face_normals = []
    for face in mesh.faces:
        nx = ny = nz = 0.0
        for k in range(len(face)):
            ax, ay, az = points[face[k]]
            bx, by, bz = points[face[(k + 1) % len(face)]]
            nx += (ay - by) * (az + bz)
            ny += (az - bz) * (ax + bx)
            nz += (ax - bx) * (ay + by)
        face_normals.append(geometry._normalise(nx, ny, nz))
    smooth = None
    if not normals_given and not mesh.flat:
        smooth = [[0.0, 0.0, 0.0] for _ in points]
        for face, n in zip(mesh.faces, face_normals):
            for v in face:
                s = smooth[v]
                s[0] += n[0]
                s[1] += n[1]
                s[2] += n[2]

    keys = _face_keys(mesh)
    pics = mesh.face_pictures + [None] * (len(mesh.faces) - len(mesh.face_pictures))
    groups: Dict[Tuple[str, Optional[str]], List[int]] = {}
    order: List[Tuple[str, Optional[str]]] = []
    for f, key in enumerate(keys):
        g = (key, pics[f])
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append(f)

    has_uv = len(mesh.point_uvs) == len(mesh.points)
    out = []
    total = 0
    dropped = 0
    for slot, g in enumerate(order):
        colour, picture = g
        builder = MeshBuilder()
        for f in groups[g]:
            face = mesh.faces[f]
            triangles = len(face) - 2
            if total + triangles > budget:
                dropped += 1
                continue
            corners = []
            uvs = mesh.corner_uvs[f] if f < len(mesh.corner_uvs) else None
            for k, v in enumerate(face):
                if mesh.flat:
                    n = face_normals[f]
                elif normals_given:
                    n = given[v]
                else:
                    n = geometry._normalise(*smooth[v])
                if uvs:
                    uv = (uvs[k][0], 1.0 - uvs[k][1])
                elif has_uv:
                    uv = (mesh.point_uvs[v][0], 1.0 - mesh.point_uvs[v][1])
                else:
                    uv = (0.0, 0.0)
                corners.append(builder.corner(points[v], n, uv, v))
            for k in range(1, len(corners) - 1):
                builder.triangle(corners[0], corners[k], corners[k + 1])
            total += triangles
        if not builder.indices:
            continue
        name = mesh.name or "Mesh"
        if len(order) > 1:
            name = "%s · %s" % (name, colour or picture or slot)
        out.append({
            "name": name,
            "positions": builder.positions,
            "normals": builder.normals,
            "uvs": builder.uvs,
            "indices": builder.indices,
            "sources": builder.sources,
            "sourceCount": len(points),
            "geometryId": slot,
            "modelId": None,
            "placement_no_fix": list(IDENTITY),
            "color": colour,
            "picture": (pictures or {}).get(picture) if picture else None,
        })
    return out, total, dropped


# -- STL ------------------------------------------------------------------------------

def _stl_binary(data: bytes) -> bool:
    if len(data) < 84:
        return False
    (count,) = struct.unpack_from("<I", data, 80)
    return 84 + 50 * count == len(data)


def read_stl(data: bytes) -> List[Mesh]:
    if _stl_binary(data):
        mesh = Mesh(data[:80].split(b"\0", 1)[0].decode("latin-1", "replace").strip()
                    .replace("solid", "", 1).strip())
        (count,) = struct.unpack_from("<I", data, 80)
        # Colour in the spare two bytes, the two ways it is done: VisCAM and
        # SolidView set bit 15 for a colour, Materialise clears it and says
        # COLOR= in the header.
        materialise = b"COLOR=" in data[:80]
        seen: Dict[bytes, int] = {}
        for i in range(count):
            values = struct.unpack_from("<12fH", data, 84 + 50 * i)
            face = []
            for k in range(3):
                p = values[3 + k * 3:6 + k * 3]
                key = struct.pack("<3f", *p)
                index = seen.get(key)
                if index is None:
                    index = seen[key] = len(mesh.points)
                    mesh.points.append(p)
                face.append(index)
            mesh.faces.append(face)
            attr = values[12]
            colour = ""
            if materialise and not attr & 0x8000:
                colour = _hex((attr & 31) / 31, (attr >> 5 & 31) / 31, (attr >> 10 & 31) / 31)
            elif not materialise and attr & 0x8000:
                colour = _hex((attr >> 10 & 31) / 31, (attr >> 5 & 31) / 31, (attr & 31) / 31)
            mesh.face_colours.append(colour)
        mesh.flat = True
        if not any(mesh.face_colours):
            mesh.face_colours = []
        return [mesh]

    text = data.decode("latin-1", "replace")
    if not text.lstrip().lower().startswith("solid"):
        raise MeshFileError("This is not an STL file.")
    meshes: List[Mesh] = []
    mesh: Optional[Mesh] = None
    seen = {}
    loop: List[int] = []
    for line in text.splitlines():
        words = line.split()
        if not words:
            continue
        word = words[0].lower()
        if word == "solid":
            mesh = Mesh(" ".join(words[1:]))
            mesh.flat = True
            meshes.append(mesh)
            seen = {}
        elif word == "vertex" and mesh is not None and len(words) >= 4:
            try:
                p = (float(words[1]), float(words[2]), float(words[3]))
            except ValueError:
                continue
            index = seen.get(p)
            if index is None:
                index = seen[p] = len(mesh.points)
                mesh.points.append(p)
            loop.append(index)
        elif word == "endloop" and mesh is not None:
            if len(loop) >= 3:
                mesh.faces.append(loop)
            loop = []
    meshes = [m for m in meshes if m.faces]
    if not meshes:
        raise MeshFileError("This STL file holds no triangles.")
    return meshes


# -- PLY ------------------------------------------------------------------------------

_PLY_TYPES = {"char": "b", "int8": "b", "uchar": "B", "uint8": "B", "short": "h", "int16": "h",
              "ushort": "H", "uint16": "H", "int": "i", "int32": "i", "uint": "I",
              "uint32": "I", "float": "f", "float32": "f", "double": "d", "float64": "d"}


def read_ply(data: bytes) -> Tuple[List[Mesh], Optional[str]]:
    """The mesh, and the picture a `comment TextureFile` names."""
    end = data.find(b"end_header")
    if not data.startswith(b"ply") or end < 0:
        raise MeshFileError("This is not a PLY file.")
    header = data[:end].decode("latin-1", "replace").replace("\r", "").split("\n")
    body = end + len(b"end_header")
    # The line ends after end_header, whichever way the file ends its lines.
    if data[body:body + 2] == b"\r\n":
        body += 2
    elif data[body:body + 1] in (b"\n", b"\r"):
        body += 1
    fmt = "ascii"
    elements: List[Tuple[str, int, List[tuple]]] = []
    texture = None
    for line in header[1:]:
        words = line.split()
        if not words:
            continue
        if words[0] == "format":
            fmt = words[1]
        elif words[0] == "comment" and len(words) > 2 and words[1].lower() == "texturefile":
            texture = " ".join(words[2:])
        elif words[0] == "element":
            elements.append((words[1], int(words[2]), []))
        elif words[0] == "property" and elements:
            if words[1] == "list":
                elements[-1][2].append(("list", words[2], words[3], words[4]))
            else:
                elements[-1][2].append(("one", words[1], words[2]))
    try:
        return _ply_body(data, body, fmt, elements, texture)
    except (StopIteration, struct.error, ValueError, KeyError) as failure:
        raise MeshFileError("This PLY file ends before its header says it does, or "
                            "its header does not describe it (%s)." % type(failure).__name__)


def _ply_body(data: bytes, body: int, fmt: str, elements: list,
              texture: Optional[str]) -> Tuple[List[Mesh], Optional[str]]:
    mesh = Mesh()
    ascii_data = fmt == "ascii"
    order = "<" if fmt == "binary_little_endian" else ">"
    tokens = iter(data[body:].split()) if ascii_data else None
    at = body
    for name, count, props in elements:
        rows: List[dict] = []
        simple = all(p[0] == "one" for p in props)
        if not ascii_data and simple and props:
            record = order + "".join(_PLY_TYPES[p[1]] for p in props)
            size = struct.calcsize(record)
            names = [p[2] for p in props]
            for values in struct.iter_unpack(record, data[at:at + size * count]):
                rows.append(dict(zip(names, values)))
            at += size * count
        else:
            for _ in range(count):
                row = {}
                for p in props:
                    if p[0] == "one":
                        if ascii_data:
                            row[p[2]] = float(next(tokens))
                        else:
                            code = order + _PLY_TYPES[p[1]]
                            (row[p[2]],) = struct.unpack_from(code, data, at)
                            at += struct.calcsize(code)
                    else:
                        if ascii_data:
                            n = int(float(next(tokens)))
                            row[p[3]] = [int(float(next(tokens))) for _ in range(n)]
                        else:
                            code = order + _PLY_TYPES[p[1]]
                            (n,) = struct.unpack_from(code, data, at)
                            at += struct.calcsize(code)
                            item = order + "%d" % n + _PLY_TYPES[p[2]]
                            row[p[3]] = list(struct.unpack_from(item, data, at))
                            at += struct.calcsize(item)
                rows.append(row)
        if name == "vertex":
            for row in rows:
                mesh.points.append((row.get("x", 0.0), row.get("y", 0.0), row.get("z", 0.0)))
            if rows and "nx" in rows[0]:
                mesh.point_normals = [(r.get("nx", 0.0), r.get("ny", 0.0), r.get("nz", 0.0))
                                      for r in rows]
            colour = next((k for k in ("red", "r", "diffuse_red") if rows and k in rows[0]), None)
            if colour:
                g = colour.replace("red", "green") if "red" in colour else "g"
                b = colour.replace("red", "blue") if "red" in colour else "b"
                scale = 255.0 if any(isinstance(r.get(colour), int) or r.get(colour, 0) > 1.0
                                     for r in rows[:64]) else 1.0
                mesh.point_colours = [(r.get(colour, 0) / scale, r.get(g, 0) / scale,
                                       r.get(b, 0) / scale) for r in rows]
            for u, v in (("s", "t"), ("u", "v"), ("texture_u", "texture_v"), ("texture_s", "texture_t")):
                if rows and u in rows[0]:
                    mesh.point_uvs = [(r.get(u, 0.0), r.get(v, 0.0)) for r in rows]
                    break
        elif name == "face":
            key = next((p[3] for p in props if p[0] == "list"), None)
            for row in rows:
                face = row.get(key) or []
                if len(face) >= 3 and all(0 <= i < len(mesh.points) for i in face):
                    mesh.faces.append(face)
            if rows and "red" in rows[0]:
                mesh.face_colours = [_hex(r.get("red", 0) / 255, r.get("green", 0) / 255,
                                          r.get("blue", 0) / 255) for r in rows]
    if not mesh.faces:
        mesh.points_only = len(mesh.points)
    return [mesh], texture


# -- OFF ------------------------------------------------------------------------------

def read_off(data: bytes) -> List[Mesh]:
    text = data.decode("latin-1", "replace")
    words = [w for line in text.splitlines() for w in line.split("#", 1)[0].split()]
    if not words or not words[0].upper().endswith("OFF"):
        raise MeshFileError("This is not an OFF file.")
    kind = words[0].upper()
    at = 1
    try:
        vertices, faces = int(words[at]), int(words[at + 1])
    except (ValueError, IndexError):
        raise MeshFileError("An OFF file without its counts.")
    at += 3
    per = 3 + (3 if "N" in kind[:-3] else 0) + (4 if "C" in kind[:-3] else 0) + \
        (2 if "ST" in kind[:-3] else 0)
    mesh = Mesh()
    try:
        for _ in range(vertices):
            row = [float(w) for w in words[at:at + per]]
            mesh.points.append((row[0], row[1], row[2]))
            at += per
        for _ in range(faces):
            n = int(words[at])
            face = [int(w) for w in words[at + 1:at + 1 + n]]
            at += 1 + n
            # The rest of the line, if any, is the face's colour; the lines
            # were joined, so it is read as far as the next face's count.
            rest = []
            while at < len(words) and len(rest) < 4 and _is_float(words[at]) and "." in words[at]:
                rest.append(float(words[at]))
                at += 1
            if len(face) >= 3 and all(0 <= i < vertices for i in face):
                mesh.faces.append(face)
                if len(rest) >= 3:
                    scale = 255.0 if max(rest[:3]) > 1.0 else 1.0
                    mesh.face_colours.append(_hex(*(c / scale for c in rest[:3])))
                else:
                    mesh.face_colours.append("")
    except (ValueError, IndexError):
        raise MeshFileError("This OFF file ends early or is damaged.")
    if not any(mesh.face_colours):
        mesh.face_colours = []
    return [mesh]


def _is_float(word: str) -> bool:
    try:
        float(word)
        return True
    except ValueError:
        return False


# -- 3MF ------------------------------------------------------------------------------

_CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
_MAT = "{http://schemas.microsoft.com/3dmanufacturing/material/2015/02}"
_PROD = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"
_UNITS = {"micron": 0.0001, "millimeter": 0.1, "centimeter": 1.0, "inch": 2.54,
          "foot": 30.48, "meter": 100.0}


def _transform(text: Optional[str]) -> List[float]:
    """A 3MF transform — twelve numbers, row vectors, translation last."""
    if not text:
        return list(IDENTITY)
    v = [float(x) for x in text.split()]
    if len(v) != 12:
        return list(IDENTITY)
    return [v[0], v[1], v[2], 0.0, v[3], v[4], v[5], 0.0, v[6], v[7], v[8], 0.0,
            v[9], v[10], v[11], 1.0]


def _colour(text: str) -> str:
    text = (text or "").strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", text):
        return text[:7].upper()
    return ""


def _parse_xml(raw: bytes) -> ET.Element:
    """XML, forgiving a prefix the file uses and never declares — the
    consortium's own samples do it — by declaring it."""
    try:
        return ET.fromstring(raw)
    except ET.ParseError as failure:
        if "unbound prefix" not in str(failure):
            raise
    text = raw.decode("utf-8", "replace")
    used = set(re.findall(r"<\/?([A-Za-z_][\w.-]*):", text)) | \
        set(re.findall(r"\s([A-Za-z_][\w.-]*):[A-Za-z_][\w.-]*=", text))
    declared = set(re.findall(r"xmlns:([A-Za-z_][\w.-]*)=", text))
    missing = sorted(used - declared - {"xml", "xmlns"})
    extra = "".join(' xmlns:%s="urn:undeclared:%s"' % (m, m) for m in missing)
    text = re.sub(r"<model\b", "<model" + extra, text, count=1)
    return ET.fromstring(text.encode("utf-8"))


class _Model3MF:
    def __init__(self, archive: zipfile.ZipFile, path: str, models: dict):
        self.archive = archive
        self.path = path
        models[path] = self
        root = _parse_xml(archive.read(path.lstrip("/")))
        self.unit = root.get("unit", "millimeter")
        self.objects: Dict[str, ET.Element] = {}
        self.colours: Dict[str, List[str]] = {}
        self.textures: Dict[str, str] = {}
        self.texgroups: Dict[str, Tuple[str, List[Tuple[float, float]]]] = {}
        resources = root.find(_CORE + "resources")
        for element in list(resources) if resources is not None else []:
            tag = element.tag
            pid = element.get("id", "")
            if tag == _CORE + "object":
                self.objects[pid] = element
            elif tag == _CORE + "basematerials":
                self.colours[pid] = [_colour(b.get("displaycolor", "")) for b in element]
            elif tag == _MAT + "colorgroup":
                self.colours[pid] = [_colour(c.get("color", "")) for c in element]
            elif tag == _MAT + "texture2d":
                self.textures[pid] = element.get("path", "")
            elif tag == _MAT + "texture2dgroup":
                self.texgroups[pid] = (element.get("texid", ""),
                                       [(float(c.get("u", 0)), float(c.get("v", 0))) for c in element])
        self.build = root.find(_CORE + "build")


def read_3mf(data: bytes) -> Tuple[List[Mesh], Dict[str, dict], float]:
    """The meshes, the pictures they name, and centimetres a unit."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise MeshFileError("This 3MF file is not a readable zip.")
    start = "/3D/3dmodel.model"
    try:
        rels = ET.fromstring(archive.read("_rels/.rels"))
        for rel in rels:
            if rel.get("Type", "").endswith("/3dmodel"):
                start = rel.get("Target", start)
    except KeyError:
        pass
    models: Dict[str, _Model3MF] = {}
    try:
        top = _Model3MF(archive, start, models)
    except (KeyError, ET.ParseError) as failure:
        raise MeshFileError("This 3MF file has no model part: %s" % failure)

    pictures: Dict[str, dict] = {}
    out: List[Mesh] = []

    def place(model: _Model3MF, object_id: str, matrix: List[float], depth: int) -> None:
        if depth > 16:
            return
        element = model.objects.get(object_id)
        if element is None:
            return
        found = element.find(_CORE + "mesh")
        if found is not None:
            out.append(_mesh_3mf(model, element, found, matrix, pictures))
        components = element.find(_CORE + "components")
        for component in list(components) if components is not None else []:
            target = model
            path = component.get(_PROD + "path")
            if path:
                target = models.get(path)
                if target is None:
                    try:
                        target = _Model3MF(archive, path, models)
                    except (KeyError, ET.ParseError):
                        continue
            place(target, component.get("objectid", ""),
                  geometry.multiply(_transform(component.get("transform")), matrix), depth + 1)

    items = list(top.build) if top.build is not None else []
    if items:
        for item in items:
            model = top
            path = item.get(_PROD + "path")
            if path:
                model = models.get(path) or _Model3MF(archive, path, models)
            place(model, item.get("objectid", ""), _transform(item.get("transform")), 0)
    else:
        for object_id in top.objects:
            place(top, object_id, list(IDENTITY), 0)
    if not out:
        raise MeshFileError("This 3MF file builds nothing.")
    return out, pictures, _UNITS.get(top.unit, 0.1)


def _mesh_3mf(model: _Model3MF, element: ET.Element, found: ET.Element, matrix: List[float],
              pictures: Dict[str, dict]) -> Mesh:
    mesh = Mesh(element.get("name", "") or "Object %s" % element.get("id", ""))
    vertices = found.find(_CORE + "vertices")
    for v in list(vertices) if vertices is not None else []:
        mesh.points.append(geometry.transform_point(
            matrix, float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0))))
    object_pid, object_index = element.get("pid"), element.get("pindex", "0")
    triangles = found.find(_CORE + "triangles")
    count = len(mesh.points)
    for t in list(triangles) if triangles is not None else []:
        try:
            face = [int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))]
        except (TypeError, ValueError):
            continue
        if not all(0 <= i < count for i in face):
            continue
        mesh.faces.append(face)
        pid = t.get("pid", object_pid)
        colour = ""
        picture = None
        uvs = None
        if pid in model.colours:
            index = t.get("p1", object_index)
            colours = model.colours[pid]
            try:
                colour = colours[int(index)]
            except (ValueError, IndexError, TypeError):
                colour = ""
        elif pid in model.texgroups:
            texid, coords = model.texgroups[pid]
            path = model.textures.get(texid, "")
            try:
                uvs = [coords[int(t.get(k, t.get("p1", object_index)))] for k in ("p1", "p2", "p3")]
            except (ValueError, IndexError, TypeError):
                uvs = None
            if path:
                picture = path
                if path not in pictures:
                    try:
                        pictures[path] = {"bytes": model.archive.read(path.lstrip("/")),
                                          "name": path.rsplit("/", 1)[-1]}
                    except KeyError:
                        pictures[path] = {"name": path.rsplit("/", 1)[-1]}
        mesh.face_colours.append(colour)
        mesh.face_pictures.append(picture)
        mesh.corner_uvs.append(uvs)
    mesh.flat = False
    return mesh


# -- the answer -----------------------------------------------------------------------

def read(kind: str, data: bytes) -> Tuple[List[Mesh], Dict[str, dict], bool, float, Optional[str]]:
    """``(meshes, pictures, z_up, cm a unit, a texture file named beside it)``."""
    if kind == "stl":
        return read_stl(data), {}, True, 0.1, None
    if kind == "ply":
        meshes, texture = read_ply(data)
        return meshes, {}, False, 1.0, texture
    if kind == "off":
        return read_off(data), {}, False, 1.0, None
    if kind == "3mf":
        meshes, pictures, unit = read_3mf(data)
        return meshes, pictures, True, unit, None
    raise MeshFileError("Not a format this reads.")


def meshes(found: List[Mesh], pictures: Dict[str, dict], z_up: bool,
           beside: Optional[str] = None, max_triangles: int = 400000) -> Tuple[List[dict], dict]:
    out: List[dict] = []
    total = dropped = points = 0
    for mesh in found:
        if beside and len(mesh.point_uvs) == len(mesh.points):
            mesh.face_pictures = [beside] * len(mesh.faces)
            pictures = dict(pictures)
            pictures.setdefault(beside, {"name": beside.rsplit("/", 1)[-1], "beside": beside})
        parts, used, lost = _parts(mesh, z_up, pictures, max_triangles - total)
        out.extend(parts)
        total += used
        dropped += lost
        points += mesh.points_only
    other = {"points without faces": points} if points else {}
    return out, {"droppedMeshes": dropped, "triangles": total, "held": total + dropped,
                 "other": other}


def summarise(kind: str, parts: List[dict], note: dict, unit: float, data: bytes) -> dict:
    binary = kind in ("3mf",) or (kind == "stl" and _stl_binary(data)) or \
        (kind == "ply" and b"format binary" in data[:400])
    return {
        "version": 0,
        "binary": binary,
        "creator": "unknown",
        "unitScale": unit,
        "upAxis": "Y",
        "frameRate": 30.0,
        "objects": len(parts),
        "connections": 0,
        "models": {"Mesh": len(parts)},
        "meshes": [{"name": mesh["name"],
                    "vertices": len(mesh["positions"]) // 3,
                    "polygons": len(mesh["indices"]) // 3,
                    "triangles": len(mesh["indices"]) // 3}
                   for mesh in parts],
        "materials": len({mesh["color"] for mesh in parts if mesh["color"]}),
        "textures": len([1 for mesh in parts if mesh.get("picture")]),
        "embedded": 0,
        "embeddedBytes": 0,
        "skins": 0,
        "clusters": 0,
        "joints": 0,
        "clips": [],
        "other": note.get("other", {}),
    }
