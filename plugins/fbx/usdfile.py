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

"""A USD stage — `.usd`, `.usda`, `.usdc`, `.usdz` — composed and drawn.

**A USD file is rarely the whole of its picture.** A kitchen is a layer that
references forty assets, each asset a layer whose payload is another layer,
whose geometry is a third, in binary, with a variant choosing which model of
teapot it is. So the reading is two steps: the layers (`usdtext`, `usdcrate`)
and then the *composition* — which layer says what about which prim — before a
single triangle can be made.

**The composition is USD's own order, cut to what a picture needs.** For each
prim the opinions are gathered strongest first: the layers of the stack
(sublayers under the layer that names them), then the selected variants, then
references and payloads, then inherited classes. A value is the first opinion
that has one. What is left out is what changes *when*, not *what*: layer
offsets, value clips, relocates — a still picture of the first frame does not
need them.

**Paths move with references.** A material bound as `</Ball/Looks/Red>`
inside the ball's own file is somewhere else once the ball is referenced into
a kitchen; every opinion remembers where its file's namespace was grafted, and
a target is carried across before it is looked up.
"""

from __future__ import annotations

import io
import math
import posixpath
import zipfile
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

import geometry
import usdcrate
import usdtext
from geometry import IDENTITY, MeshBuilder
from usdcrate import AssetPath, ListOp, Reference, TimeSamples


class UsdError(Exception):
    pass


def is_usd(data: bytes) -> bool:
    return data[:8] == usdcrate.MAGIC or data[:5] == b"#usda" or (
        data[:2] == b"PK" and b".usd" in data[:200])


# -- layers, and where they come from ------------------------------------------------

class Layer:
    def __init__(self, key: str, specs: Dict[str, dict], binary: bool):
        self.key = key
        self.specs = specs
        self.binary = binary
        root = specs.get("/", {}).get("fields", {})
        self.meta = root
        subs = root.get("subLayers") or []
        self.sublayers = [str(s) for s in (subs if isinstance(subs, list) else [subs])]
        self.default_prim = str(root.get("defaultPrim") or "")


class Resolver:
    """Asset paths, made into keys, and keys into bytes.

    A key is a URL, or a URL and a member after `#` for a file inside a
    `.usdz` — whose references are to other members, and never leave it.
    """

    def __init__(self, read: Callable[[str], Optional[bytes]]):
        self._read = read
        self._bytes: Dict[str, Optional[bytes]] = {}
        self._zips: Dict[str, zipfile.ZipFile] = {}

    def resolve(self, anchor: str, asset: str) -> str:
        asset = asset.replace("\\", "/")
        if "#" in anchor:
            outer, member = anchor.split("#", 1)
            folder = posixpath.dirname(member)
            joined = posixpath.normpath(posixpath.join(folder, asset)).lstrip("./") \
                if not asset.startswith("/") else asset.lstrip("/")
            return outer + "#" + joined
        if asset.startswith("/") or (len(asset) > 1 and asset[1] == ":"):
            return "file://" + quote(asset if asset.startswith("/") else "/" + asset)
        base = anchor.rsplit("/", 1)[0]
        scheme, _, rest = base.partition("://")
        host, _, path = rest.partition("/")
        joined = posixpath.normpath(posixpath.join("/" + unquote(path), asset))
        return "%s://%s%s" % (scheme, host, quote(joined))

    def read(self, key: str) -> Optional[bytes]:
        if key in self._bytes:
            return self._bytes[key]
        if "#" in key:
            outer, member = key.split("#", 1)
            archive = self._zip(outer)
            data = None
            if archive is not None:
                try:
                    data = archive.read(member)
                except KeyError:
                    data = None
        else:
            try:
                data = self._read(key)
            except Exception:  # noqa: BLE001 - a missing file is a missing file
                data = None
        self._bytes[key] = data
        return data

    def _zip(self, url: str) -> Optional[zipfile.ZipFile]:
        found = self._zips.get(url)
        if found is None:
            data = self.read(url)
            if not data or data[:2] != b"PK":
                return None
            found = zipfile.ZipFile(io.BytesIO(data))
            self._zips[url] = found
        return found

    def first_layer(self, url: str) -> str:
        """The key of the layer a file opens as: itself, or a `.usdz`'s first member."""
        data = self.read(url)
        if data and data[:2] == b"PK":
            archive = self._zip(url)
            names = [n for n in archive.namelist()
                     if n.lower().rsplit(".", 1)[-1] in ("usd", "usda", "usdc")] if archive else []
            if not names:
                raise UsdError("This .usdz holds no USD layer.")
            return url + "#" + names[0]
        return url


def parse_layer(key: str, data: bytes) -> Layer:
    if data[:8] == usdcrate.MAGIC:
        return Layer(key, usdcrate.Crate(data).layer(), True)
    if data[:5] == b"#usda":
        return Layer(key, usdtext.parse(data), False)
    raise UsdError("%s is not a USD layer." % key.rsplit("/", 1)[-1])


# -- the stage ------------------------------------------------------------------------

class Source:
    """One opinion about a prim: the layer, the path in it, and how the layer's
    namespace was grafted onto the stage's (`graft_from` became `graft_to`)."""

    __slots__ = ("layer", "path", "graft_from", "graft_to")

    def __init__(self, layer: Layer, path: str, graft_from: str = "/", graft_to: str = "/"):
        self.layer = layer
        self.path = path
        self.graft_from = graft_from
        self.graft_to = graft_to

    @property
    def spec(self) -> Optional[dict]:
        return self.layer.specs.get(self.path)

    def carry(self, target: str) -> str:
        """A path written in this layer, as a path on the stage."""
        frm, to = self.graft_from, self.graft_to
        if frm == "/" and to == "/":
            return target
        if target == frm or target.startswith(frm + "/") or target.startswith(frm + "."):
            rest = target[len(frm):]
            return (to.rstrip("/") + rest) if rest else to
        return target


class Prim:
    def __init__(self, stage: "Stage", path: str, sources: List[Source]):
        self.stage = stage
        self.path = path
        self.name = path.rsplit("/", 1)[-1] or "/"
        self.sources = sources
        self._children: Optional[List["Prim"]] = None

    def field(self, name: str, default=None):
        for source in self.sources:
            spec = source.spec
            if spec is not None and name in spec["fields"]:
                value = spec["fields"].get(name)
                if value is not None:
                    return value
        return default

    @property
    def type(self) -> str:
        return str(self.field("typeName", "") or "")

    @property
    def defined(self) -> bool:
        """Whether any opinion says `def` — an `over` alone makes nothing."""
        return any((s.spec or {}).get("fields", {}).get("specifier") == "def"
                   for s in self.sources)

    @property
    def abstract(self) -> bool:
        for source in self.sources:
            said = (source.spec or {}).get("fields", {}).get("specifier")
            if said in ("def", "class"):
                return said == "class"
        return False

    def attribute(self, name: str, default=None):
        value, _ = self.attribute_with_source(name)
        return default if value is None else value

    def attribute_with_source(self, name: str) -> Tuple[object, Optional[Source]]:
        for source in self.sources:
            spec = source.layer.specs.get(source.path + "." + name)
            if spec is None:
                continue
            fields = spec["fields"]
            samples = fields.get("timeSamples")
            if isinstance(samples, TimeSamples) and samples:
                return usdcrate.sample(samples, self.stage.start), source
            if "default" in fields:
                value = fields.get("default")
                if value is not None:
                    return value, source
        return None, None

    def attribute_field(self, name: str, field: str, default=None):
        for source in self.sources:
            spec = source.layer.specs.get(source.path + "." + name)
            if spec is not None and field in spec["fields"]:
                return spec["fields"].get(field)
        return default

    def targets(self, name: str, field: str = "targetPaths") -> List[str]:
        """A relationship's targets, or an attribute's connections, on the stage."""
        for source in self.sources:
            spec = source.layer.specs.get(source.path + "." + name)
            if spec is None:
                continue
            found = spec["fields"].get(field)
            if isinstance(found, ListOp):
                items = found.applied()
            elif isinstance(found, list):
                items = found
            else:
                continue
            if items:
                return [source.carry(self._absolute(source, str(t))) for t in items]
        return []

    @staticmethod
    def _absolute(source: Source, target: str) -> str:
        if target.startswith("/"):
            return target
        return usdtext._resolve(source.path, target)

    def property_names(self) -> List[str]:
        out: List[str] = []
        for source in self.sources:
            spec = source.spec
            for name in (spec or {}).get("fields", {}).get("properties", []) or []:
                if name not in out:
                    out.append(name)
        return out

    @property
    def children(self) -> List["Prim"]:
        if self._children is None:
            names: List[str] = []
            for source in self.sources:
                spec = source.spec
                for name in (spec or {}).get("fields", {}).get("primChildren", []) or []:
                    if name not in names:
                        names.append(name)
            out = []
            for name in names:
                path = usdtext.child_path(self.path, name)
                child_sources = [Source(s.layer, usdtext.child_path(s.path, name),
                                        s.graft_from, s.graft_to)
                                 for s in self.sources
                                 if usdtext.child_path(s.path, name) in s.layer.specs]
                out.append(Prim(self.stage, path, self.stage.expand(path, child_sources)))
            self._children = out
        return self._children


class Stage:
    MAX_DEPTH = 24

    def __init__(self, resolver: Resolver, url: str,
                 report: Optional[Callable[[float], None]] = None):
        self.resolver = resolver
        self.layers: Dict[str, Optional[Layer]] = {}
        self.missing: List[str] = []
        self.report = report
        root_key = resolver.first_layer(url)
        self.root = self.layer(root_key)
        if self.root is None:
            raise UsdError("The file could not be read as USD.")
        meta = self.root.meta
        self.up_axis = str(meta.get("upAxis") or "Y")
        mpu = meta.get("metersPerUnit")
        self.meters_per_unit = float(mpu) if isinstance(mpu, (int, float)) and mpu > 0 else 0.01
        start = meta.get("startTimeCode")
        self.start = float(start) if isinstance(start, (int, float)) else None
        self.fps = float(meta.get("timeCodesPerSecond") or meta.get("framesPerSecond") or 24)
        self.variants_chosen: Dict[str, str] = {}
        self._depth = 0
        self.pseudo_root = Prim(self, "/", self.stage_sources(self.root, "/"))

    def layer(self, key: str) -> Optional[Layer]:
        if key in self.layers:
            return self.layers[key]
        self.layers[key] = None  # a cycle reads as a missing layer
        data = self.resolver.read(key)
        if not data:
            self.missing.append(key)
            return None
        if data[:2] == b"PK" and "#" not in key:
            key_inner = self.resolver.first_layer(key)
            found = self.layer(key_inner)
            self.layers[key] = found
            return found
        try:
            found = parse_layer(key, data)
        except Exception:  # noqa: BLE001 - one broken layer is not a broken stage
            self.missing.append(key)
            return None
        self.layers[key] = found
        if self.report is not None:
            self.report(len(self.layers))
        return found

    def stack(self, layer: Layer, seen: Optional[set] = None) -> List[Layer]:
        """A layer and its sublayers, strongest first."""
        seen = seen if seen is not None else set()
        if layer.key in seen:
            return []
        seen.add(layer.key)
        out = [layer]
        for asset in layer.sublayers:
            sub = self.layer(self.resolver.resolve(layer.key, asset))
            if sub is not None:
                out.extend(self.stack(sub, seen))
        return out

    def stage_sources(self, layer: Layer, path: str, graft_from: str = "/",
                      graft_to: str = "/") -> List[Source]:
        return [Source(member, path, graft_from, graft_to) for member in self.stack(layer)
                if path in member.specs]

    def expand(self, path: str, local: List[Source]) -> List[Source]:
        """A prim's opinions with its arcs followed: variants, references and
        payloads, inherits — each weaker than what came before it."""
        if self._depth > self.MAX_DEPTH:
            return local
        self._depth += 1
        try:
            out = list(local)
            # Variants: a selection is the strongest opinion's, set by set.
            names: List[str] = []
            chosen: Dict[str, str] = {}
            for source in out:
                fields = (source.spec or {}).get("fields", {})
                listed = fields.get("variantSetNames")
                for name in (listed.applied() if isinstance(listed, ListOp) else listed or []):
                    if name not in names:
                        names.append(name)
                for name, value in (fields.get("variantSelection") or {}).items():
                    chosen.setdefault(name, value)
            for name in names:
                selection = chosen.get(name)
                if not selection:
                    # No selection authored: the first variant, the way a
                    # viewer shows a model rather than nothing.
                    for source in out:
                        set_spec = source.layer.specs.get("%s{%s=}" % (source.path, name))
                        if set_spec:
                            children = set_spec["fields"].get("variantChildren") or []
                            if children:
                                selection = children[0]
                                break
                if not selection:
                    continue
                self.variants_chosen.setdefault("%s:%s" % (path, name), selection)
                added = []
                for source in list(out):
                    variant = "%s{%s=%s}" % (source.path, name, selection)
                    if variant in source.layer.specs:
                        added.append(Source(source.layer, variant, source.graft_from,
                                            source.graft_to))
                out.extend(self.expand(path, added) if added else [])

            arcs: List[Source] = []
            for source in list(out):
                fields = (source.spec or {}).get("fields", {})
                for key in ("references", "payload"):
                    found = fields.get(key)
                    items = found.applied() if isinstance(found, ListOp) else (
                        [found] if isinstance(found, Reference) else found or [])
                    for item in items:
                        arcs.extend(self._arc(source, path, item))
            for source in list(out):
                fields = (source.spec or {}).get("fields", {})
                for key in ("inherits", "specializes"):
                    found = fields.get(key)
                    items = found.applied() if isinstance(found, ListOp) else found or []
                    for item in items:
                        target = source.carry(str(item))
                        inherited = self.stage_sources(self.root, target, target, path)
                        arcs.extend(self.expand(path, inherited))
            out.extend(arcs)
            return out
        finally:
            self._depth -= 1

    def _arc(self, source: Source, path: str, item) -> List[Source]:
        if isinstance(item, Reference):
            asset, prim = item.asset, item.prim
        elif isinstance(item, (str, AssetPath)):
            asset, prim = str(item), ""
        else:
            return []
        if asset:
            layer = self.layer(self.resolver.resolve(source.layer.key, asset))
            if layer is None:
                return []
        else:
            layer = source.layer
        if not prim:
            prim = "/" + layer.default_prim if layer.default_prim else ""
            if not prim:
                children = layer.specs.get("/", {}).get("fields", {}).get("primChildren") or []
                prim = "/" + children[0] if children else ""
        if not prim:
            return []
        found = self.stage_sources(layer, prim, prim, path)
        return self.expand(path, found)

    def walk(self):
        """Every prim that is drawn, parents first, with its world matrix."""
        # The view reads no axis setting and has Y up: a Z-up stage is tipped
        # back a quarter turn about X, as the FBX reader does.
        top = _Z_UP_TO_Y_UP if self.up_axis.upper() == "Z" else IDENTITY
        todo = [(child, top) for child in reversed(self.pseudo_root.children)]
        while todo:
            prim, parent = todo.pop()
            if prim.abstract or not prim.defined:
                continue
            if prim.field("active") is False:
                continue
            if prim.attribute("visibility") == "invisible":
                continue
            if prim.attribute("purpose") == "guide":
                continue
            local, reset = transform(prim)
            world = local if reset else geometry.multiply(local, parent)
            yield prim, world
            for child in reversed(prim.children):
                todo.append((child, world))


# -- transforms -----------------------------------------------------------------------

#: (x, y, z) to (x, z, -y), met by a row vector.
_Z_UP_TO_Y_UP = [1.0, 0, 0, 0, 0, 0, -1.0, 0, 0, 1.0, 0, 0, 0, 0, 0, 1.0]

def _rotation(axis: str, degrees: float) -> List[float]:
    c = math.cos(math.radians(degrees))
    s = math.sin(math.radians(degrees))
    if axis == "X":
        return [1, 0, 0, 0, 0, c, s, 0, 0, -s, c, 0, 0, 0, 0, 1]
    if axis == "Y":
        return [c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0, 0, 0, 0, 1]
    return [c, s, 0, 0, -s, c, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def _quaternion(q) -> List[float]:
    w, x, y, z = (float(v) for v in q)
    n = math.sqrt(w * w + x * x + y * y + z * z) or 1.0
    w, x, y, z = w / n, x / n, y / n, z / n
    return [1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w), 0,
            2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w), 0,
            2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y), 0,
            0, 0, 0, 1]


def _op_matrix(op: str, value) -> Optional[List[float]]:
    kind = op.split(":")[1] if ":" in op else op
    if value is None:
        return None
    if kind == "translate":
        x, y, z = (float(v) for v in value)
        return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1]
    if kind == "scale":
        if isinstance(value, (int, float)):
            value = (value, value, value)
        x, y, z = (float(v) for v in value)
        return [x, 0, 0, 0, 0, y, 0, 0, 0, 0, z, 0, 0, 0, 0, 1]
    if kind in ("rotateX", "rotateY", "rotateZ"):
        return _rotation(kind[-1], float(value))
    if kind.startswith("rotate") and len(kind) == 9:
        # rotateXYZ: X first, then Y, then Z — met by a row vector, so the
        # matrices multiply in the order the letters are read.
        angles = dict(zip("XYZ", (float(v) for v in value)))
        out = IDENTITY
        for axis in kind[6:]:
            out = geometry.multiply(out, _rotation(axis, angles[axis]))
        return out
    if kind == "orient":
        return _quaternion(value)
    if kind == "transform":
        flat = [float(v) for row in value for v in (row if isinstance(row, (list, tuple)) else [row])]
        return flat if len(flat) == 16 else None
    return None


def invert(m: List[float]) -> List[float]:
    a = [m[i * 4:(i + 1) * 4] + [1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]
    for col in range(4):
        pivot = max(range(col, 4), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return list(IDENTITY)
        a[col], a[pivot] = a[pivot], a[col]
        p = a[col][col]
        a[col] = [v / p for v in a[col]]
        for row in range(4):
            if row != col and a[row][col]:
                f = a[row][col]
                a[row] = [v - f * w for v, w in zip(a[row], a[col])]
    return [a[i][4 + j] for i in range(4) for j in range(4)]


def transform(prim: Prim) -> Tuple[List[float], bool]:
    """A prim's own matrix, and whether it ignores its parents'."""
    order = prim.attribute("xformOpOrder")
    if not order:
        return list(IDENTITY), False
    out = list(IDENTITY)
    reset = False
    for op in order:
        op = str(op)
        if op == "!resetXformStack!":
            reset = True
            out = list(IDENTITY)
            continue
        inverted = op.startswith("!invert!")
        name = op[8:] if inverted else op
        matrix = _op_matrix(name, prim.attribute(name))
        if matrix is None:
            continue
        if inverted:
            matrix = invert(matrix)
        out = geometry.multiply(matrix, out)
    return out, reset


# -- meshes ---------------------------------------------------------------------------

_UV_NAMES = ("st", "st0", "st_0", "UVMap", "uv", "uv0", "map1", "UVW", "texcoord")


def _primvar(prim: Prim, name: str):
    """A primvar's values, spread by its indices if it has them, and how."""
    values = prim.attribute("primvars:" + name)
    if values is None:
        return None, None
    indices = prim.attribute("primvars:%s:indices" % name)
    if indices:
        values = [values[i] if 0 <= i < len(values) else values[0] for i in indices]
    how = prim.attribute_field("primvars:" + name, "interpolation", "constant")
    return values, str(how or "constant")


def _uv_primvar(prim: Prim):
    for name in _UV_NAMES:
        values, how = _primvar(prim, name)
        if values:
            return values, how
    for prop in prim.property_names():
        if prop.startswith("primvars:") and not prop.endswith(":indices"):
            type_name = str(prim.attribute_field(prop, "typeName", "") or "")
            if type_name.startswith("texCoord2"):
                values, how = _primvar(prim, prop[9:])
                if values:
                    return values, how
    return None, None


def _colour(values) -> str:
    if not values:
        return ""
    first = values[0] if isinstance(values, list) else values
    if not isinstance(first, (list, tuple)) or len(first) < 3:
        return ""
    # Linear, as USD keeps colours; the host's picture is sRGB.
    return "#%02X%02X%02X" % tuple(
        max(0, min(255, int(round(_to_srgb(float(c)) * 255)))) for c in first[:3])


def _to_srgb(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _polygons(prim: Prim):
    points = prim.attribute("points")
    counts = prim.attribute("faceVertexCounts")
    indices = prim.attribute("faceVertexIndices")
    if not points or not counts or not indices:
        return None
    faces = []
    at = 0
    for n in counts:
        n = int(n)
        faces.append(list(indices[at:at + n]))
        at += n
    return [tuple(float(v) for v in p) for p in points], faces


def _mesh_parts(stage: Stage, prim: Prim, world: List[float], budget: int,
                pictures: "Materials") -> Tuple[List[dict], int]:
    found = _polygons(prim)
    if found is None:
        return [], 0
    points, faces = found
    left_handed = prim.attribute("orientation") == "leftHanded"
    world_points = [geometry.transform_point(world, *p) for p in points]
    # A mirrored matrix turns every face inside out; so does leftHanded.
    flip = left_handed != (_determinant(world) < 0)

    normals, normals_how = prim.attribute("normals"), None
    if normals:
        normals_how = str(prim.attribute_field("normals", "interpolation", "vertex") or "vertex")
    else:
        normals, normals_how = _primvar(prim, "normals")
    uvs, uv_how = _uv_primvar(prim)
    colours, _ = _primvar(prim, "displayColor")

    face_normals = []
    for face in faces:
        nx = ny = nz = 0.0
        for k in range(len(face)):
            ax, ay, az = world_points[face[k]] if face[k] < len(world_points) else (0, 0, 0)
            bx, by, bz = world_points[face[(k + 1) % len(face)]] \
                if face[(k + 1) % len(face)] < len(world_points) else (0, 0, 0)
            nx += (ay - by) * (az + bz)
            ny += (az - bz) * (ax + bx)
            nz += (ax - bx) * (ay + by)
        n = geometry._normalise(nx, ny, nz)
        face_normals.append((-n[0], -n[1], -n[2]) if flip else n)
    smooth = None
    if not normals:
        smooth = [[0.0, 0.0, 0.0] for _ in world_points]
        for face, n in zip(faces, face_normals):
            for v in face:
                if v < len(smooth):
                    s = smooth[v]
                    s[0] += n[0]
                    s[1] += n[1]
                    s[2] += n[2]

    groups = _subsets(prim, len(faces))
    parts = []
    used = 0
    corner_at = 0
    corners_of = []
    for face in faces:
        corners_of.append(corner_at)
        corner_at += len(face)
    for face_list, material in groups:
        builder = MeshBuilder()
        for f in face_list:
            face = faces[f]
            if len(face) < 3 or any(v >= len(world_points) for v in face):
                continue
            corners = []
            for k, v in enumerate(face):
                corner = corners_of[f] + k
                if normals:
                    n = _pick(normals, normals_how, v, f, corner)
                    n = geometry._normalise(*geometry.transform_direction(world, *n)) \
                        if n else face_normals[f]
                    if left_handed:
                        n = (-n[0], -n[1], -n[2])
                else:
                    n = geometry._normalise(*smooth[v])
                uv = _pick(uvs, uv_how, v, f, corner) if uvs else None
                uv = (float(uv[0]), 1.0 - float(uv[1])) if uv else (0.0, 0.0)
                corners.append(builder.corner(world_points[v], n, uv, v))
            for k in range(1, len(corners) - 1):
                if flip:
                    builder.triangle(corners[0], corners[k + 1], corners[k])
                else:
                    builder.triangle(corners[0], corners[k], corners[k + 1])
        triangles = len(builder.indices) // 3
        if not triangles:
            continue
        if used + triangles > budget:
            break
        used += triangles
        colour, picture = pictures.of(material)
        if not colour:
            colour = _colour(colours) or "#9A9A9A"
        parts.append({
            "name": prim.name,
            "positions": builder.positions,
            "normals": builder.normals,
            "uvs": builder.uvs,
            "indices": builder.indices,
            "sources": builder.sources,
            "sourceCount": len(points),
            "geometryId": hash(prim.path) & 0x7FFFFFFF,
            "modelId": None,
            "placement_no_fix": list(IDENTITY),
            "color": colour,
            "picture": picture,
        })
    return parts, used


def _pick(values, how: str, vertex: int, face: int, corner: int):
    if how == "faceVarying":
        at = corner
    elif how == "uniform":
        at = face
    elif how == "constant":
        at = 0
    else:
        at = vertex
    return values[at] if 0 <= at < len(values) else None


def _determinant(m: List[float]) -> float:
    return (m[0] * (m[5] * m[10] - m[6] * m[9]) - m[1] * (m[4] * m[10] - m[6] * m[8])
            + m[2] * (m[4] * m[9] - m[5] * m[8]))


def _subsets(prim: Prim, count: int) -> List[Tuple[List[int], Optional[str]]]:
    """The faces by the material they wear: a GeomSubset each, the rest the mesh's."""
    own = bound_material(prim)
    taken = set()
    out = []
    for child in prim.children:
        if child.type != "GeomSubset":
            continue
        if (child.attribute("familyName") or "materialBind") != "materialBind":
            continue
        faces = [int(i) for i in (child.attribute("indices") or []) if 0 <= int(i) < count]
        if not faces:
            continue
        taken.update(faces)
        out.append((faces, bound_material(child, inherit=False) or own))
    rest = [f for f in range(count) if f not in taken]
    if rest:
        out.insert(0, (rest, own))
    return out


def bound_material(prim: Prim, inherit: bool = True) -> Optional[str]:
    path = prim.path
    current: Optional[Prim] = prim
    while current is not None:
        for name in ("material:binding", "material:binding:preview", "material:binding:full"):
            targets = current.targets(name)
            if targets:
                return targets[0]
        if not inherit:
            return None
        current = current.stage.parent_of(current.path) if hasattr(current.stage, "parent_of") else None
    _ = path
    return None


class Materials:
    """What each material looks like: a colour, and a picture if it has one."""

    def __init__(self, stage: Stage):
        self.stage = stage
        self._known: Dict[str, Tuple[str, Optional[dict]]] = {}
        self.textures: set = set()
        self.names: set = set()

    def of(self, path: Optional[str]) -> Tuple[str, Optional[dict]]:
        if not path:
            return "", None
        if path in self._known:
            return self._known[path]
        self.names.add(path)
        answer = ("", None)
        material = self.stage.prim_at(path)
        if material is not None:
            shader = self._surface(material)
            if shader is not None:
                answer = self._preview(shader)
        self._known[path] = answer
        return answer

    def _surface(self, material: Prim) -> Optional[Prim]:
        for output in ("outputs:surface", "outputs:ri:surface", "outputs:mtlx:surface"):
            targets = material.targets(output, "connectionPaths")
            if targets:
                shader = self.stage.prim_at(targets[0].split(".", 1)[0])
                # Through a node graph's output to the shader behind it.
                if shader is not None and shader.type == "NodeGraph":
                    inner = shader.targets(targets[0].split(".", 1)[1], "connectionPaths")
                    shader = self.stage.prim_at(inner[0].split(".", 1)[0]) if inner else None
                if shader is not None:
                    return shader
        return None

    def _preview(self, shader: Prim) -> Tuple[str, Optional[dict]]:
        colour = ""
        picture = None
        for name in ("inputs:diffuseColor", "inputs:base_color", "inputs:baseColor"):
            value = shader.attribute(name)
            if isinstance(value, (list, tuple)) and len(value) >= 3:
                colour = _colour([value])
            links = shader.targets(name, "connectionPaths")
            if links:
                texture = self.stage.prim_at(links[0].split(".", 1)[0])
                if texture is not None:
                    picture = self._texture(texture)
            if colour or picture:
                break
        return colour, picture

    def _texture(self, texture: Prim) -> Optional[dict]:
        for name in ("inputs:file", "inputs:filename"):
            value, source = texture.attribute_with_source(name)
            if value and source is not None:
                asset = str(value)
                if "<UDIM>" in asset:
                    asset = asset.replace("<UDIM>", "1001")
                key = self.stage.resolver.resolve(source.layer.key, asset)
                self.textures.add(key)
                data = self.stage.resolver.read(key)
                name_only = asset.replace("\\", "/").rsplit("/", 1)[-1]
                if data:
                    return {"bytes": data, "name": name_only}
                # Not where the layer says: the model's own reader looks for
                # it by name beside the file, and counts it missing if not.
                return {"name": name_only}
        return None


def _prim_at(stage: Stage, path: str) -> Optional[Prim]:
    if not path or not path.startswith("/"):
        return None
    current = stage.pseudo_root
    for name in [p for p in path.split("/") if p]:
        found = None
        for child in current.children:
            if child.name == name:
                found = child
                break
        if found is None:
            return None
        current = found
    return current


def _parent_of(stage: Stage, path: str) -> Optional[Prim]:
    parent = path.rsplit("/", 1)[0]
    if not parent:
        return None
    return _prim_at(stage, parent)


Stage.prim_at = _prim_at  # type: ignore[attr-defined]
Stage.parent_of = _parent_of  # type: ignore[attr-defined]


# -- primitives -----------------------------------------------------------------------

def _shape(kind: str, prim: Prim, world: List[float]) -> Optional[Tuple[list, list]]:
    """Cube, Sphere, Cylinder, Cone, Capsule, Plane: points and faces made here."""
    axis = str(prim.attribute("axis") or "Z")
    if kind == "Cube":
        h = float(prim.attribute("size") or 2.0) / 2
        pts = [(-h, -h, -h), (h, -h, -h), (h, h, -h), (-h, h, -h),
               (-h, -h, h), (h, -h, h), (h, h, h), (-h, h, h)]
        faces = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [2, 3, 7, 6], [1, 2, 6, 5], [0, 4, 7, 3]]
        return pts, faces
    if kind == "Sphere":
        r = float(prim.attribute("radius") or 1.0)
        return _lathe([(r * math.sin(math.pi * i / 16), -r * math.cos(math.pi * i / 16))
                       for i in range(17)], "Y", closed_ends=False)
    if kind in ("Cylinder", "Cone", "Capsule"):
        r = float(prim.attribute("radius") or (0.5 if kind == "Capsule" else 1.0))
        h = float(prim.attribute("height") or (1.0 if kind == "Capsule" else 2.0)) / 2
        if kind == "Cylinder":
            profile = [(0.0, -h), (r, -h), (r, h), (0.0, h)]
        elif kind == "Cone":
            profile = [(0.0, -h), (r, -h), (0.0, h)]
        else:
            profile = [(r * math.sin(math.pi / 2 * i / 6), -h - r * math.cos(math.pi / 2 * i / 6))
                       for i in range(7)]
            profile += [(r * math.cos(math.pi / 2 * i / 6), h + r * math.sin(math.pi / 2 * i / 6))
                        for i in range(7)]
        return _lathe(profile, axis, closed_ends=False)
    if kind == "Plane":
        w = float(prim.attribute("width") or 2.0) / 2
        ln = float(prim.attribute("length") or 2.0) / 2
        pts = {"X": [(0, -w, -ln), (0, w, -ln), (0, w, ln), (0, -w, ln)],
               "Y": [(-w, 0, -ln), (-w, 0, ln), (w, 0, ln), (w, 0, -ln)],
               "Z": [(-w, -ln, 0), (w, -ln, 0), (w, ln, 0), (-w, ln, 0)]}[axis if axis in "XYZ" else "Z"]
        return pts, [[0, 1, 2, 3]]
    return None


def _lathe(profile: List[Tuple[float, float]], axis: str, closed_ends: bool,
           segments: int = 24) -> Tuple[list, list]:
    points = []
    for radius, height in profile:
        for s in range(segments):
            a = 2 * math.pi * s / segments
            u, v = radius * math.cos(a), radius * math.sin(a)
            if axis == "X":
                points.append((height, u, v))
            elif axis == "Y":
                points.append((v, height, u))
            else:
                points.append((u, v, height))
    faces = []
    for ring in range(len(profile) - 1):
        for s in range(segments):
            a = ring * segments + s
            b = ring * segments + (s + 1) % segments
            faces.append([a, b, b + segments, a + segments])
    return points, faces


class _Shape:
    """Enough of a prim for `_mesh_parts` to take a made-up shape through it."""

    def __init__(self, prim: Prim, points: list, faces: list):
        self._prim = prim
        self._points = points
        self._faces = faces
        self.name = prim.name
        self.path = prim.path
        self.stage = prim.stage
        self.children: list = []

    def attribute(self, name: str, default=None):
        if name == "points":
            return self._points
        if name == "faceVertexCounts":
            return [len(f) for f in self._faces]
        if name == "faceVertexIndices":
            return [v for f in self._faces for v in f]
        if name in ("normals", "orientation") or name.startswith("primvars:") and \
                not name.startswith("primvars:displayColor"):
            return default
        return self._prim.attribute(name, default)

    def attribute_field(self, name: str, field: str, default=None):
        return self._prim.attribute_field(name, field, default)

    def targets(self, name: str, field: str = "targetPaths"):
        return self._prim.targets(name, field)

    def property_names(self):
        return []


# -- the answer -----------------------------------------------------------------------

DRAWN = ("Mesh", "Cube", "Sphere", "Cylinder", "Cone", "Capsule", "Plane")
NOT_DRAWN = {"Points": "point clouds", "BasisCurves": "curves", "NurbsCurves": "curves",
             "NurbsPatch": "NURBS surfaces", "PointInstancer": "point instancers",
             "Volume": "volumes", "TetMesh": "tetrahedral meshes"}


def read(url: str, read_url: Callable[[str], Optional[bytes]],
         report: Optional[Callable[[float], None]] = None) -> Stage:
    return Stage(Resolver(read_url), url, report)


def meshes(stage: Stage, max_triangles: int = 400000) -> Tuple[List[dict], dict]:
    out: List[dict] = []
    total = 0
    dropped = 0
    other: Dict[str, int] = {}
    kinds: Dict[str, int] = {}
    materials = Materials(stage)
    stage.materials = materials  # type: ignore[attr-defined]
    for prim, world in stage.walk():
        kind = prim.type
        if kind:
            kinds[kind] = kinds.get(kind, 0) + 1
        if kind in NOT_DRAWN:
            what = NOT_DRAWN[kind]
            other[what] = other.get(what, 0) + 1
            continue
        if kind not in DRAWN:
            continue
        if kind == "Mesh":
            # Skipped whole when it would not fit, so that the smaller meshes
            # after it are still drawn rather than everything past the cap.
            counts = prim.attribute("faceVertexCounts") or []
            if total + sum(max(0, int(n) - 2) for n in counts) > max_triangles:
                dropped += 1
                continue
        target = prim
        if kind != "Mesh":
            made = _shape(kind, prim, world)
            if made is None:
                continue
            target = _Shape(prim, *made)
        parts, used = _mesh_parts(stage, target, world, max_triangles - total, materials)
        out.extend(parts)
        total += used
    return out, {"droppedMeshes": dropped, "triangles": total, "held": total,
                 "other": other, "kinds": kinds}


def _variant_counts(chosen: Dict[str, str]) -> List[Tuple[str, str, int, str]]:
    """``(set, choice, prims, first prim)``: a kitchen of 125 cheerios says so once."""
    grouped: Dict[Tuple[str, str], List[str]] = {}
    for where, choice in chosen.items():
        prim, _, name = where.rpartition(":")
        grouped.setdefault((name, choice), []).append(prim)
    return [(name, choice, len(prims), prims[0])
            for (name, choice), prims in sorted(grouped.items())]


def summarise(stage: Stage, parts: List[dict], note: dict, size: int) -> dict:
    meta = stage.root.meta
    materials = getattr(stage, "materials", None)
    layers = [k for k, v in stage.layers.items() if v is not None]
    return {
        "version": 0,
        "binary": stage.root.binary,
        "creator": str(meta.get("documentation") or meta.get("doc") or "USD"),
        "usdLayers": len(set(id(v) for v in stage.layers.values() if v is not None)),
        "usdLayersMissing": sorted(set(k.rsplit("/", 1)[-1] for k in stage.missing)),
        "usdVariants": _variant_counts(stage.variants_chosen),
        "droppedMeshes": note.get("droppedMeshes", 0),
        "unitScale": stage.meters_per_unit * 100.0,
        "upAxis": stage.up_axis,
        "frameRate": stage.fps,
        "objects": sum(note.get("kinds", {}).values()),
        "connections": 0,
        "models": note.get("kinds", {}),
        "meshes": [{"name": mesh["name"],
                    "vertices": len(mesh["positions"]) // 3,
                    "polygons": len(mesh["indices"]) // 3,
                    "triangles": len(mesh["indices"]) // 3}
                   for mesh in parts],
        "materials": len(materials.names) if materials else 0,
        "textures": len(materials.textures) if materials else 0,
        "embedded": 0,
        "embeddedBytes": 0,
        "skins": note.get("kinds", {}).get("SkelRoot", 0),
        "clusters": 0,
        "joints": 0,
        "clips": [],
        "animCurves": 0,
        "references": [],
        "other": note.get("other", {}),
        "layerKeys": layers,
    }
