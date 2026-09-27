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

"""Houdini geometry — `.geo`, `.bgeo`, `.bgeo.sc`, and the `.geo` of before
Houdini 12 — as the polygons in it.

**Four ways of writing one thing.** Since Houdini 12 a geometry file is a JSON
document (`.geo`), or the same document in Houdini's binary JSON (`.bgeo`,
beginning `\\x7fNSJb`), or that binary compressed in Blosc blocks inside
SideFX's own `scf1` container (`.bgeo.sc`). Before that, `.geo` was a text
format of its own, beginning `PGEOMETRY V5`. All four are read here into
points, polygons as rings of points, and the attributes a preview can use —
`P`, and `N`, `uv` and `Cd` where they are on the points or the vertices.

**The binary JSON** is JSON with its values tagged: a byte saying what comes
next — an integer of a given width, a float of 16, 32 or 64 bits, a string or
a reference to one defined earlier, an array of one type packed tight.

**The compression** is Blosc — blocks, each split into byte streams by a
shuffle and compressed, here with LZ4 — and both are undone here in plain
Python, so nothing is installed. It is the slowest part of reading a large
`.bgeo.sc`, and it says how far it has got.

What is not a polygon — a curve, a volume, a packed primitive, a particle —
is counted and not drawn.
"""

from __future__ import annotations

import json
import math
import struct
from typing import Callable, Dict, List, Optional, Tuple

import geometry
from geometry import MeshBuilder, IDENTITY


class HoudiniError(ValueError):
    pass


# -- the binary JSON ----------------------------------------------------------------

_MAGIC = b"\x7fNSJb"
_MAGIC_SWAPPED = b"\x7fbJSN"


class _Binary:
    def __init__(self, data: bytes, at: int, little: bool):
        self.data = data
        self.at = at
        self.e = "<" if little else ">"
        self.tokens: Dict[int, str] = {}

    def byte(self) -> int:
        b = self.data[self.at]
        self.at += 1
        return b

    def _num(self, fmt: str, size: int):
        (v,) = struct.unpack_from(self.e + fmt, self.data, self.at)
        self.at += size
        return v

    def length(self) -> int:
        n = self.byte()
        if n < 0xF1:
            return n
        if n == 0xF2:
            return self._num("H", 2)
        if n == 0xF4:
            return self._num("I", 4)
        if n == 0xF8:
            return self._num("Q", 8)
        raise HoudiniError("A length the binary JSON does not define.")

    def string(self) -> str:
        n = self.length()
        s = self.data[self.at:self.at + n].decode("utf-8", "replace")
        self.at += n
        return s

    def value(self, jid: Optional[int] = None):
        while True:
            if jid is None:
                jid = self.byte()
            if jid == 0x2B:          # a token defined for later
                ident = self.length()
                self.tokens[ident] = self.string()
                jid = None
                continue
            if jid == 0x2D:          # a token undefined
                self.tokens.pop(self.length(), None)
                jid = None
                continue
            break
        if jid == 0x5B:
            out = []
            while True:
                nxt = self.byte()
                if nxt == 0x5D:
                    return out
                out.append(self.value(nxt))
        if jid == 0x7B:
            out = {}
            while True:
                nxt = self.byte()
                if nxt == 0x7D:
                    return out
                key = self.value(nxt)
                out[key] = self.value()
        if jid == 0x26:
            return self.tokens.get(self.length(), "")
        if jid == 0x27:
            return self.string()
        if jid in _SCALARS:
            fmt, size = _SCALARS[jid]
            v = self._num(fmt, size)
            return v
        if jid == 0x10:
            return bool(self.byte())
        if jid == 0x30:
            return False
        if jid == 0x31:
            return True
        if jid == 0x00:
            return None
        if jid == 0x40:
            return self.uniform()
        raise HoudiniError("An unknown value (0x%02x) in the binary JSON." % jid)

    def uniform(self) -> list:
        kind = self.byte()
        count = self.length()
        if kind == 0x10:            # booleans, a bit each
            words = (count + 31) // 32
            bits = struct.unpack_from(self.e + "%dI" % words, self.data, self.at)
            self.at += 4 * words
            return [bool(bits[i // 32] >> (i % 32) & 1) for i in range(count)]
        fmt, size = _SCALARS[kind]
        values = struct.unpack_from(self.e + "%d%s" % (count, fmt), self.data, self.at)
        self.at += size * count
        return list(values)


_SCALARS = {
    0x11: ("b", 1), 0x12: ("h", 2), 0x13: ("i", 4), 0x14: ("q", 8),
    0x18: ("e", 2), 0x19: ("f", 4), 0x1A: ("d", 8),
    0x21: ("B", 1), 0x22: ("H", 2),
}


def binary_json(data: bytes):
    if data.startswith(_MAGIC):
        little = True
    elif data.startswith(_MAGIC_SWAPPED):
        little = False
    else:
        raise HoudiniError("This is not Houdini's binary JSON.")
    return _Binary(data, 5, little).value()


# -- Blosc, and LZ4 under it ---------------------------------------------------------------

def _lz4_block(src: bytes, size: int) -> bytes:
    """One LZ4 block into [size] bytes."""
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        token = src[i]
        i += 1
        length = token >> 4
        if length == 15:
            while True:
                b = src[i]
                i += 1
                length += b
                if b != 255:
                    break
        out += src[i:i + length]
        i += length
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        match = token & 15
        if match == 15:
            while True:
                b = src[i]
                i += 1
                match += b
                if b != 255:
                    break
        match += 4
        start = len(out) - offset
        if offset >= match:
            out += out[start:start + match]
        else:
            for k in range(match):
                out.append(out[start + k])
    return bytes(out[:size])


def _codec(code: int, src: bytes, size: int) -> bytes:
    if code == 1:
        return _lz4_block(src, size)
    if code == 3:
        import zlib
        return zlib.decompress(src)
    if code == 0:
        raise HoudiniError("This file is compressed with BloscLZ, which is not read here.")
    raise HoudiniError("This file is compressed with a Blosc codec (%d) not read here." % code)


def _blosc(chunk: bytes) -> Tuple[bytes, int]:
    """One Blosc chunk, decompressed, and the bytes it took."""
    version, _lz, flags, typesize = chunk[0], chunk[1], chunk[2], chunk[3]
    nbytes, blocksize, cbytes = struct.unpack_from("<III", chunk, 4)
    if flags & 0x02 or cbytes == nbytes + 16:          # stored, not compressed
        return chunk[16:16 + nbytes], cbytes
    code = (flags >> 5) & 7
    shuffle = flags & 0x01
    bitshuffle = flags & 0x04
    if bitshuffle:
        raise HoudiniError("This file is bit-shuffled, which is not read here.")
    blocks = (nbytes + blocksize - 1) // blocksize
    starts = struct.unpack_from("<%di" % blocks, chunk, 16)
    split = not (flags & 0x10) and shuffle and typesize <= 16 and blocksize // typesize >= 128 \
        if version >= 2 else (typesize <= 16 and blocksize // typesize >= 128 and not flags & 0x10)
    out = bytearray()
    for b in range(blocks):
        size = min(blocksize, nbytes - b * blocksize)
        at = starts[b]
        streams = typesize if split and size == blocksize else 1
        each = size // streams
        block = bytearray()
        for _ in range(streams):
            (csize,) = struct.unpack_from("<i", chunk, at)
            at += 4
            src = chunk[at:at + csize]
            at += csize
            block += src if csize == each else _codec(code, src, each)
        if shuffle and typesize > 1:
            block = _unshuffle(bytes(block), typesize)
        out += block
    return bytes(out), cbytes


def _unshuffle(block: bytes, typesize: int) -> bytearray:
    count = len(block) // typesize
    out = bytearray(len(block))
    for t in range(typesize):
        out[t:count * typesize:typesize] = block[t * count:(t + 1) * count]
    tail = count * typesize
    out[tail:] = block[tail:]
    return out


def unpack_sc(data: bytes, report: Optional[Callable[[float], None]] = None) -> bytes:
    """An `scf1` file's contents: Blosc chunks one after another."""
    if not data.startswith(b"scf1"):
        raise HoudiniError("This is not a compressed Houdini file.")
    at = 12
    out = bytearray()
    total = len(data)
    while at + 16 <= total:
        if data[at] not in (1, 2):
            break
        try:
            chunk, used = _blosc(data[at:])
        except (struct.error, IndexError):
            break
        out += chunk
        at += used
        if report is not None:
            report(at / total)
        if out.startswith(_MAGIC) is False and out[:5] not in (_MAGIC, _MAGIC_SWAPPED):
            break
    return bytes(out)


# -- the document --------------------------------------------------------------------------

def read(data: bytes, report: Optional[Callable[[float], None]] = None):
    """The geometry document, whichever of the four it was written as. The
    old text format comes back already as polygons."""
    if data.startswith(b"scf1"):
        data = unpack_sc(data, report)
    if data.startswith(_MAGIC) or data.startswith(_MAGIC_SWAPPED):
        return binary_json(data)
    head = data[:64].lstrip()
    if head.startswith(b"PGEOMETRY"):
        return _classic(data.decode("latin-1"))
    if head.startswith(b"["):
        return json.loads(data.decode("utf-8", "replace"))
    raise HoudiniError("This is not Houdini geometry.")


def _pairs(sequence) -> dict:
    """Houdini's JSON writes a map as a flat list: key, value, key, value."""
    if isinstance(sequence, dict):
        return sequence
    out = {}
    if isinstance(sequence, list):
        for k in range(0, len(sequence) - 1, 2):
            if isinstance(sequence[k], str):
                out[sequence[k]] = sequence[k + 1]
    return out


# -- the old text format ------------------------------------------------------------------

def _classic(text: str) -> dict:
    """`PGEOMETRY V5`: counts, the points, then one primitive a line — or a
    `Run` of them — written as `Poly 4 < 0 1 2 3`, `<` closed and `:` open.
    Handed back in the shape the JSON document is read in."""
    lines = text.splitlines()
    head: Dict[str, int] = {}
    at = 0
    while at < len(lines) and not lines[at].startswith(("PointAttrib", "VertexAttrib")) \
            and not _is_point(lines[at]):
        words = lines[at].split()
        for k in range(0, len(words) - 1, 2):
            if words[k + 1].lstrip("-").isdigit():
                head[words[k]] = int(words[k + 1])
        at += 1
    # The attribute definitions, however many lines they take: the points
    # begin at the first line that is four numbers.
    while at < len(lines) and not _is_point(lines[at]):
        at += 1
    points = []
    for _ in range(head.get("NPoints", 0)):
        words = lines[at].split("(")[0].split()
        at += 1
        x, y, z = (float(w) for w in words[:3])
        wgt = float(words[3]) if len(words) > 3 else 1.0
        points.append((x / wgt, y / wgt, z / wgt) if wgt not in (0.0, 1.0) else (x, y, z))
    faces: List[List[int]] = []
    other: Dict[str, int] = {}
    run_kind = None
    run_left = 0
    while at < len(lines):
        line = lines[at].strip()
        at += 1
        if not line or line.startswith(("beginExtra", "endExtra", "prender")):
            continue
        words = line.split()
        if words[0] in ("PrimitiveAttrib", "DetailAttrib"):
            # Its definitions follow, one a line: names, not primitives.
            at += head.get("NPrimAttrib" if words[0] == "PrimitiveAttrib" else "NAttrib", 0)
            continue
        if words[0] == "Run" and len(words) >= 3:
            run_left, run_kind = int(words[1]), words[2]
            continue
        if run_left > 0:
            run_left -= 1
            kind, rest = run_kind, words
        else:
            kind, rest = words[0], words[1:]
        if kind == "Poly" and len(rest) >= 2 and rest[0].isdigit():
            n = int(rest[0])
            ring = [int(w) for w in rest[2:2 + n] if w.lstrip("-").isdigit()]
            if rest[1] == "<" and len(ring) >= 3:
                faces.append(ring)
            else:
                other["open polygons"] = other.get("open polygons", 0) + 1
        elif kind == "Part":
            other["particles"] = other.get("particles", 0) + int(rest[0]) if rest else 1
        elif kind[0].isalpha():
            other[kind] = other.get(kind, 0) + 1
    return {"classic": True, "points": points, "faces": faces, "other": other}


def _is_point(line: str) -> bool:
    words = line.split()
    return len(words) >= 4 and all(_numeric(w) for w in words[:4])


def _numeric(word: str) -> bool:
    try:
        float(word)
        return True
    except ValueError:
        return False


# -- attributes ---------------------------------------------------------------------------

def _values(body: dict, count: int) -> Optional[List[tuple]]:
    """A numeric attribute's values, one tuple per element, whichever of its
    three forms it was written in — tuples, arrays of components, or pages."""
    values = _pairs(body.get("values"))
    size = int(values.get("size") or body.get("size") or 1)
    if "tuples" in values:
        return [tuple(t) if isinstance(t, (list, tuple)) else (t,) for t in values["tuples"]]
    if "arrays" in values:
        return list(zip(*values["arrays"]))
    raw = values.get("rawpagedata")
    if raw is None:
        return None
    pagesize = int(values.get("pagesize") or 1024)
    packing = values.get("packing") or [size]
    flags = values.get("constantpageflags") or []
    out: List[list] = [[0.0] * size for _ in range(count)]
    at = 0
    pages = (count + pagesize - 1) // pagesize
    for page in range(pages):
        first = page * pagesize
        n = min(pagesize, count - first)
        offset = 0
        for group, width in enumerate(packing):
            constant = (group < len(flags) and isinstance(flags[group], list)
                        and page < len(flags[group]) and flags[group][page])
            if constant:
                chunk = raw[at:at + width]
                at += width
                for i in range(n):
                    out[first + i][offset:offset + width] = chunk
            else:
                chunk = raw[at:at + n * width]
                at += n * width
                for i in range(n):
                    out[first + i][offset:offset + width] = chunk[i * width:(i + 1) * width]
            offset += width
    return [tuple(v) for v in out]


def _attributes(section, count: int) -> Dict[str, List[tuple]]:
    out: Dict[str, List[tuple]] = {}
    for entry in section or []:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        head, body = _pairs(entry[0]), _pairs(entry[1])
        if head.get("type") != "numeric" or head.get("name") not in ("P", "N", "uv", "Cd", "Pw"):
            continue
        found = _values(body, count)
        if found is not None and len(found) >= count:
            out[head["name"]] = found[:count]
    return out


# -- primitives ---------------------------------------------------------------------------

#: The short names Houdini 18 and later write a run of primitives under.
_RUNS = {"p_r": "Polygon_run", "c_r": "PolygonCurve_run", "t_r": "Tetrahedron_run"}


def _rle(pairs: list) -> List[int]:
    out = []
    for k in range(0, len(pairs) - 1, 2):
        out += [int(pairs[k])] * int(pairs[k + 1])
    return out


def primitives(doc: dict) -> Tuple[List[List[int]], Dict[str, int]]:
    """Closed polygons as rings of vertex numbers, and a count of the rest."""
    faces: List[List[int]] = []
    other: Dict[str, int] = {}

    def count(kind: str, n: int = 1) -> None:
        other[kind] = other.get(kind, 0) + n

    for entry in doc.get("primitives") or []:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        head, body = _pairs(entry[0]), entry[1]
        kind = _RUNS.get(head.get("type"), head.get("type"))
        if kind == "run":
            runtype = head.get("runtype")
            closed = _pairs(head.get("uniformfields")).get("closed", True)
            if runtype == "Poly" and "vertex" in (head.get("varyingfields") or []):
                for prim in body:
                    ring = prim[0] if isinstance(prim, list) and prim else []
                    if closed and len(ring) >= 3:
                        faces.append(list(ring))
                    else:
                        count("open polygons")
            else:
                count(runtype or "other", len(body) if isinstance(body, list) else 1)
            continue
        fields = _pairs(body)
        if kind == "Poly":
            ring = fields.get("vertex") or []
            if fields.get("closed", True) and len(ring) >= 3:
                faces.append(list(ring))
            else:
                count("open polygons")
        elif kind in ("Polygon_run", "PolygonCurve_run", "Tetrahedron_run"):
            start = int(fields.get("startvertex", fields.get("s_v", 0)))
            prims = int(fields.get("nprimitives", fields.get("n_p", 0)))
            if kind == "Tetrahedron_run":
                sizes = [4] * prims
            elif "nvertices_rle" in fields or "r_v" in fields:
                sizes = _rle(fields.get("nvertices_rle") or fields.get("r_v"))
            else:
                sizes = [int(n) for n in (fields.get("nvertices") or fields.get("n_v") or [])]
            at = start
            if kind == "Polygon_run":
                for n in sizes:
                    if n >= 3:
                        faces.append(list(range(at, at + n)))
                    at += n
            elif kind == "Tetrahedron_run":
                faces += _tet_surface(start, prims)
                count("tetrahedra", prims)
            else:
                count("curves", len(sizes))
        elif kind == "Tetrahedron":
            ring = fields.get("vertex") or []
            if len(ring) == 4:
                faces += _tet_faces(ring)
            count("tetrahedra")
        else:
            count(kind or "other")
    return faces, other


def _tet_faces(v: List[int]) -> List[List[int]]:
    a, b, c, d = v
    return [[a, b, c], [a, d, b], [a, c, d], [b, d, c]]


def _tet_surface(start: int, prims: int) -> List[List[int]]:
    """The faces of a run of tetrahedra that only one of them has: its skin.
    Returned as vertex rings, marked for the surface pass to keep only once
    and wind outwards — see :func:`_skin`."""
    return [["tet", start + 4 * t] for t in range(prims)]


def _skin(faces: List[list], pointref: List[int], points: List[tuple]) -> List[List[int]]:
    """Tetrahedra into their outer faces, wound to face out."""
    plain = [f for f in faces if not (f and f[0] == "tet")]
    tets = [f[1] for f in faces if f and f[0] == "tet"]
    if not tets:
        return plain
    seen: Dict[tuple, Optional[List[int]]] = {}
    for base in tets:
        v = [base, base + 1, base + 2, base + 3]
        p = [pointref[k] for k in v]
        cx = sum(points[q][0] for q in p) / 4
        cy = sum(points[q][1] for q in p) / 4
        cz = sum(points[q][2] for q in p) / 4
        for tri in ((0, 1, 2), (0, 1, 3), (0, 2, 3), (1, 2, 3)):
            key = tuple(sorted(p[t] for t in tri))
            if key in seen:
                seen[key] = None
                continue
            ring = [v[t] for t in tri]
            a, b, c = (points[pointref[k]] for k in ring)
            n = ((b[1] - a[1]) * (c[2] - a[2]) - (b[2] - a[2]) * (c[1] - a[1]),
                 (b[2] - a[2]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[2] - a[2]),
                 (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
            out = (a[0] - cx) * n[0] + (a[1] - cy) * n[1] + (a[2] - cz) * n[2]
            # Houdini's polygons wind clockwise; so do these, outwards.
            seen[key] = list(reversed(ring)) if out > 0 else ring
    return plain + [ring for ring in seen.values() if ring is not None]


# -- the answer ---------------------------------------------------------------------------

def meshes(doc, max_triangles: int = 400000,
           report: Optional[Callable[[float], None]] = None) -> Tuple[List[dict], dict]:
    if isinstance(doc, dict) and doc.get("classic"):
        points = doc["points"]
        pointref = list(range(len(points)))
        faces = doc["faces"]
        other = doc["other"]
        attrs: Dict[str, List[tuple]] = {}
        vattrs: Dict[str, List[tuple]] = {}
        # A classic face lists points; make them the vertices.
        rings = []
        for face in faces:
            rings.append(list(face))
        faces = rings
    else:
        d = _pairs(doc)
        npoints = int(d.get("pointcount") or 0)
        nverts = int(d.get("vertexcount") or 0)
        topology = _pairs(d.get("topology"))
        pointref = list(_pairs(topology.get("pointref")).get("indices") or range(nverts))
        sections = _pairs(d.get("attributes"))
        attrs = _attributes(sections.get("pointattributes"), npoints)
        vattrs = _attributes(sections.get("vertexattributes"), nverts)
        if report is not None:
            report(0.7)
        if "P" not in attrs:
            return [], {"droppedMeshes": 0, "triangles": 0, "held": 0,
                        "other": {"no positions": 1}}
        points = [p[:3] for p in attrs["P"]]
        faces, other = primitives(d)
        faces = _skin(faces, pointref, points)

    triangles = sum(len(f) - 2 for f in faces)
    note = {"droppedMeshes": 0, "triangles": 0, "held": triangles, "other": other}
    if not faces:
        return [], note
    if triangles > max_triangles:
        keep, total = [], 0
        for f in faces:
            if total + len(f) - 2 > max_triangles:
                break
            keep.append(f)
            total += len(f) - 2
        faces = keep
        note["droppedMeshes"] = 1

    normals_v = vattrs.get("N")
    normals_p = attrs.get("N")
    uvs_v = vattrs.get("uv")
    uvs_p = attrs.get("uv")

    # Houdini winds a polygon clockwise seen from its front; the host counts
    # the other way, so every ring is read backwards.
    rings = [list(reversed(f)) for f in faces]
    smooth: Optional[List[list]] = None
    if normals_v is None and normals_p is None:
        smooth = [[0.0, 0.0, 0.0] for _ in points]
        for ring in rings:
            n = _newell([points[pointref[v]] for v in ring])
            for v in ring:
                s = smooth[pointref[v]]
                s[0] += n[0]
                s[1] += n[1]
                s[2] += n[2]
    builder = MeshBuilder()
    step = max(1, len(rings) // 50)
    for at, ring in enumerate(rings):
        if report is not None and at % step == 0:
            report(0.7 + 0.3 * at / len(rings))
        corners = []
        for v in ring:
            p = pointref[v]
            if normals_v is not None:
                n = normals_v[v][:3]
            elif normals_p is not None:
                n = normals_p[p][:3]
            else:
                n = smooth[p]
            uv = (uvs_v[v] if uvs_v is not None else uvs_p[p] if uvs_p is not None else (0.0, 0.0))
            corners.append(builder.corner(tuple(points[p]), geometry._normalise(*n),
                                          (uv[0], 1.0 - uv[1]), p))
        for k in range(1, len(corners) - 1):
            builder.triangle(corners[0], corners[k], corners[k + 1])
    note["triangles"] = len(builder.indices) // 3
    colour = ""
    cd = attrs.get("Cd")
    if cd:
        r = sum(c[0] for c in cd) / len(cd)
        g = sum(c[1] for c in cd) / len(cd)
        b = sum(c[2] for c in cd) / len(cd)
        colour = "#%02X%02X%02X" % tuple(max(0, min(255, int(round(x * 255)))) for x in (r, g, b))
    part = {
        "name": "geometry",
        "positions": builder.positions,
        "normals": builder.normals,
        "uvs": builder.uvs,
        "indices": builder.indices,
        "sources": builder.sources,
        "sourceCount": len(points),
        "geometryId": 0,
        "modelId": None,
        "placement_no_fix": list(IDENTITY),
        "color": colour,
        "picture": None,
    }
    return [part], note


def _newell(ring: List[tuple]) -> Tuple[float, float, float]:
    nx = ny = nz = 0.0
    for k in range(len(ring)):
        ax, ay, az = ring[k]
        bx, by, bz = ring[(k + 1) % len(ring)]
        nx += (ay - by) * (az + bz)
        ny += (az - bz) * (ax + bx)
        nz += (ax - bx) * (ay + by)
    return geometry._normalise(nx, ny, nz)


def summarise(doc, parts: List[dict], note: dict, size: int) -> dict:
    info = {} if isinstance(doc, dict) and doc.get("classic") else _pairs(_pairs(doc).get("info"))
    d = {} if isinstance(doc, dict) and doc.get("classic") else _pairs(doc)
    other = note.get("other") or {}
    return {
        "version": 0,
        "binary": not (isinstance(doc, dict) and doc.get("classic")),
        "creator": info.get("software") or "Houdini",
        "houdiniVersion": d.get("fileversion", ""),
        "unitScale": 100.0,
        "upAxis": "Y",
        "frameRate": 24.0,
        "objects": int(d.get("primitivecount") or 0),
        "connections": 0,
        "models": {k: v for k, v in other.items()},
        "meshes": [{"name": m["name"], "vertices": len(m["positions"]) // 3,
                    "polygons": len(m["indices"]) // 3, "triangles": len(m["indices"]) // 3}
                   for m in parts],
        "materials": 0, "textures": 0, "embedded": 0, "embeddedBytes": 0,
        "skins": 0, "clusters": 0, "joints": 0, "clips": [],
        "points": int(d.get("pointcount") or 0),
        "other": other,
    }
