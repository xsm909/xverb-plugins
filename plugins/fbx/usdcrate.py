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

"""USD's binary layer — the *crate*, `PXR-USDC` — read without USD.

A crate is a table of contents and six sections: the **tokens** every name is
made of, the **strings**, the **fields** (a name and a value), the **field
sets** (runs of fields, one run a spec), the **paths**, and the **specs**
themselves (a path, its field set, and what kind of thing it is). What comes
out of here is the same thing the text reader gives: a dict of specs by path,
each its kind and its fields — so everything after this does not care which
of the two the layer was.

**Two compressions, nested.** Sections and big arrays are LZ4 blocks in
USD's own chunked wrapper (`TfFastCompression`), and integer arrays are
*first* delta-coded with two bits a value saying how wide the delta is — a
common delta, one byte, two, or four — and then LZ4'd. Both are written out
below, from `pxr/usd/sdf/crateFile.cpp` and `integerCoding.cpp`; there is no
lz4 in Python's standard library.

**A value is eight bytes, a `ValueRep`**: the type in bits 48–55, three flags
(array, inlined, compressed) at the top, and a payload — the value itself
when it is small enough to be inlined, otherwise where in the file it is.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

MAGIC = b"PXR-USDC"


class CrateError(Exception):
    pass


# -- LZ4, and USD's wrapper round it -------------------------------------------------

def lz4_block(src: bytes, size_hint: int = 0) -> bytes:
    """One LZ4 block, decompressed."""
    out = bytearray()
    at = 0
    end = len(src)
    while at < end:
        token = src[at]
        at += 1
        literal = token >> 4
        if literal == 15:
            while True:
                more = src[at]
                at += 1
                literal += more
                if more != 255:
                    break
        if literal:
            out += src[at:at + literal]
            at += literal
        if at >= end:
            break
        offset = src[at] | (src[at + 1] << 8)
        at += 2
        if offset == 0:
            raise CrateError("A damaged LZ4 block: a match at distance nought.")
        length = token & 15
        if length == 15:
            while True:
                more = src[at]
                at += 1
                length += more
                if more != 255:
                    break
        length += 4
        start = len(out) - offset
        if start < 0:
            raise CrateError("A damaged LZ4 block: a match before the start.")
        if offset >= length:
            out += out[start:start + length]
        else:
            # An overlapping match repeats the last `offset` bytes.
            piece = out[start:]
            whole, rest = divmod(length, offset)
            out += piece * whole + piece[:rest]
    return bytes(out)


def fast_decompress(src: bytes) -> bytes:
    """`TfFastCompression`: a count of chunks, then each chunk's LZ4 block."""
    if not src:
        return b""
    chunks = src[0]
    if chunks == 0:
        return lz4_block(src[1:])
    out = []
    at = 1
    for _ in range(chunks):
        (size,) = struct.unpack_from("<i", src, at)
        at += 4
        out.append(lz4_block(src[at:at + size]))
        at += size
    return b"".join(out)


def decode_ints(buf: bytes, count: int, wide: bool = False) -> List[int]:
    """USD's integer coding: a common delta, two bits a value, then the deltas.

    For 32-bit integers the widths are the common value, 1, 2 and 4 bytes;
    for 64-bit, the common value, 2, 4 and 8.
    """
    if count == 0:
        return []
    if wide:
        (common,) = struct.unpack_from("<q", buf, 0)
        at = 8
        widths = (0, 2, 4, 8)
        formats = (None, "<h", "<i", "<q")
    else:
        (common,) = struct.unpack_from("<i", buf, 0)
        at = 4
        widths = (0, 1, 2, 4)
        formats = (None, "<b", "<h", "<i")
    codes_at = at
    data_at = at + (count * 2 + 7) // 8
    out = [0] * count
    previous = 0
    unpack = struct.unpack_from
    for i in range(count):
        code = (buf[codes_at + (i >> 2)] >> ((i & 3) * 2)) & 3
        if code == 0:
            delta = common
        else:
            (delta,) = unpack(formats[code], buf, data_at)
            data_at += widths[code]
        previous += delta
        out[i] = previous
    return out


# -- the file -----------------------------------------------------------------------

#: `CrateDataTypes`, in the order the file numbers them.
TYPES = [
    "Invalid", "Bool", "UChar", "Int", "UInt", "Int64", "UInt64", "Half", "Float",
    "Double", "String", "Token", "AssetPath", "Matrix2d", "Matrix3d", "Matrix4d",
    "Quatd", "Quatf", "Quath", "Vec2d", "Vec2f", "Vec2h", "Vec2i", "Vec3d", "Vec3f",
    "Vec3h", "Vec3i", "Vec4d", "Vec4f", "Vec4h", "Vec4i", "Dictionary",
    "TokenListOp", "StringListOp", "PathListOp", "ReferenceListOp", "IntListOp",
    "Int64ListOp", "UIntListOp", "UInt64ListOp", "PathVector", "TokenVector",
    "Specifier", "Permission", "Variability", "VariantSelectionMap", "TimeSamples",
    "Payload", "DoubleVector", "LayerOffsetVector", "StringVector", "ValueBlock",
    "Value", "UnregisteredValue", "UnregisteredValueListOp", "PayloadListOp",
    "TimeCode", "PathExpression", "Relocates", "Spline", "AnimationBlock",
]

#: The scalar types by their shape: struct format of one component, and count.
_SHAPES = {
    "Bool": ("?", 1), "UChar": ("B", 1), "Int": ("i", 1), "UInt": ("I", 1),
    "Int64": ("q", 1), "UInt64": ("Q", 1), "Half": ("e", 1), "Float": ("f", 1),
    "Double": ("d", 1), "TimeCode": ("d", 1),
    "Matrix2d": ("d", 4), "Matrix3d": ("d", 9), "Matrix4d": ("d", 16),
    "Quatd": ("d", 4), "Quatf": ("f", 4), "Quath": ("e", 4),
    "Vec2d": ("d", 2), "Vec2f": ("f", 2), "Vec2h": ("e", 2), "Vec2i": ("i", 2),
    "Vec3d": ("d", 3), "Vec3f": ("f", 3), "Vec3h": ("e", 3), "Vec3i": ("i", 3),
    "Vec4d": ("d", 4), "Vec4f": ("f", 4), "Vec4h": ("e", 4), "Vec4i": ("i", 4),
}

#: `SdfSpecType`.
SPEC_TYPES = ["Unknown", "Attribute", "Connection", "Expression", "Mapper", "MapperArg",
              "Prim", "PseudoRoot", "Relationship", "RelationshipTarget", "Variant",
              "VariantSet"]

SPECIFIERS = ["def", "over", "class"]


def _real_first(q: tuple) -> tuple:
    """A quaternion as the text format writes it, real part first; the file
    keeps `GfQuat`'s memory order, the imaginary part first."""
    return (q[3], q[0], q[1], q[2])


class AssetPath(str):
    """An `@asset@` value, told apart from a plain string."""


class Reference:
    """A reference or a payload: the file, the prim in it, and nothing else we use."""

    __slots__ = ("asset", "prim")

    def __init__(self, asset: str, prim: str):
        self.asset = asset
        self.prim = prim

    def __repr__(self):
        return "Reference(%r, %r)" % (self.asset, self.prim)


class ListOp:
    """A list edit: what it says explicitly, and what it adds before and after."""

    __slots__ = ("explicit", "items", "prepended", "appended", "deleted")

    def __init__(self):
        self.explicit = False
        self.items: list = []
        self.prepended: list = []
        self.appended: list = []
        self.deleted: list = []

    def applied(self) -> list:
        """The result of the edit over nothing — enough for a single layer."""
        if self.explicit:
            return list(self.items)
        out = list(self.prepended) + list(self.items) + list(self.appended)
        return [x for x in out if x not in self.deleted]

    def __repr__(self):
        return "ListOp(%r)" % self.applied()


class TimeSamples(dict):
    """Values by time."""


class Crate:
    def __init__(self, data: bytes):
        if data[:8] != MAGIC:
            raise CrateError("This is not a binary USD file.")
        self.data = data
        self.version = tuple(data[8:11])
        if self.version < (0, 4, 0):
            raise CrateError("This binary USD file is from before 2017 (crate %d.%d.%d), "
                             "which is not read." % self.version)
        (toc,) = struct.unpack_from("<q", data, 16)
        (count,) = struct.unpack_from("<Q", data, toc)
        self.sections: Dict[str, Tuple[int, int]] = {}
        at = toc + 8
        for _ in range(count):
            name = data[at:at + 16].split(b"\0", 1)[0].decode("ascii", "replace")
            start, size = struct.unpack_from("<qq", data, at + 16)
            self.sections[name] = (start, size)
            at += 32
        self.tokens = self._tokens()
        self.strings = self._strings()
        self.fields = self._fields()
        self.fieldsets = self._fieldsets()
        self.paths = self._paths()
        self.specs = self._specs()

    # -- the sections --

    def _section(self, name: str) -> int:
        found = self.sections.get(name)
        if found is None:
            raise CrateError("This binary USD file has no %s section." % name)
        return found[0]

    def _compressed_ints(self, at: int, count: int, wide: bool = False) -> Tuple[List[int], int]:
        (size,) = struct.unpack_from("<Q", self.data, at)
        at += 8
        raw = fast_decompress(self.data[at:at + size])
        return decode_ints(raw, count, wide), at + size

    def _tokens(self) -> List[str]:
        at = self._section("TOKENS")
        count, raw_size, packed = struct.unpack_from("<QQQ", self.data, at)
        raw = fast_decompress(self.data[at + 24:at + 24 + packed])
        words = raw[:raw_size].split(b"\0")
        return [w.decode("utf-8", "replace") for w in words[:count]]

    def _strings(self) -> List[int]:
        at = self._section("STRINGS")
        (count,) = struct.unpack_from("<Q", self.data, at)
        return list(struct.unpack_from("<%dI" % count, self.data, at + 8))

    def _fields(self) -> List[Tuple[int, int]]:
        at = self._section("FIELDS")
        (count,) = struct.unpack_from("<Q", self.data, at)
        names, at = self._compressed_ints(at + 8, count)
        (size,) = struct.unpack_from("<Q", self.data, at)
        raw = fast_decompress(self.data[at + 8:at + 8 + size])
        reps = struct.unpack_from("<%dQ" % count, raw, 0)
        return list(zip(names, reps))

    def _fieldsets(self) -> List[int]:
        at = self._section("FIELDSETS")
        (count,) = struct.unpack_from("<Q", self.data, at)
        found, _ = self._compressed_ints(at + 8, count)
        return found

    def _paths(self) -> List[str]:
        at = self._section("PATHS")
        (count,) = struct.unpack_from("<Q", self.data, at)
        (encoded,) = struct.unpack_from("<Q", self.data, at + 8)
        indexes, at = self._compressed_ints(at + 16, encoded)
        elements, at = self._compressed_ints(at, encoded)
        jumps, at = self._compressed_ints(at, encoded)
        paths = [""] * count
        tokens = self.tokens

        # Iterative form of crate's recursive walk: a stack of (index, parent)
        # still to be visited, siblings pushed as they are found.
        todo = [(0, "")]
        while todo:
            index, parent = todo.pop()
            while True:
                this = index
                index += 1
                if not parent:
                    path = "/"
                else:
                    element = elements[this]
                    name = tokens[-element if element < 0 else element]
                    if element < 0:
                        path = parent + "." + name
                    elif name.startswith("{") or parent.endswith("}"):
                        path = parent + name
                    else:
                        path = (parent if parent != "/" else "") + "/" + name
                paths[indexes[this]] = path
                jump = jumps[this]
                has_child = jump > 0 or jump == -1
                has_sibling = jump >= 0
                if has_child:
                    if has_sibling:
                        todo.append((this + jump, parent))
                    parent = path
                elif not has_sibling:
                    break
        return paths

    def _specs(self) -> List[Tuple[int, int, int]]:
        at = self._section("SPECS")
        (count,) = struct.unpack_from("<Q", self.data, at)
        paths, at = self._compressed_ints(at + 8, count)
        sets, at = self._compressed_ints(at, count)
        kinds, _ = self._compressed_ints(at, count)
        return list(zip(paths, sets, kinds))

    # -- values --

    def string(self, index: int) -> str:
        return self.tokens[self.strings[index]]

    def value(self, rep: int):
        kind = (rep >> 48) & 0xFF
        is_array = bool(rep >> 63 & 1)
        inlined = bool(rep >> 62 & 1)
        compressed = bool(rep >> 61 & 1)
        payload = rep & ((1 << 48) - 1)
        name = TYPES[kind] if kind < len(TYPES) else "?"
        if inlined:
            return self._inlined(name, payload)
        if is_array:
            return self._array(name, payload, compressed)
        return self._at(name, payload)

    def _inlined(self, name: str, payload: int):
        low = payload & 0xFFFFFFFF
        if name == "Bool":
            return bool(low)
        if name in ("UChar", "UInt", "Specifier", "Permission", "Variability"):
            if name == "Specifier":
                return SPECIFIERS[low] if low < 3 else "def"
            return low
        if name == "Int":
            return struct.unpack("<i", struct.pack("<I", low))[0]
        if name in ("Float", "Double", "TimeCode"):
            # An inlined double is written as a float when it is one exactly.
            return struct.unpack("<f", struct.pack("<I", low))[0]
        if name == "Half":
            return struct.unpack("<e", struct.pack("<H", low & 0xFFFF))[0]
        if name == "Token":
            return self.tokens[low]
        if name == "String":
            return self.string(low)
        if name == "AssetPath":
            return AssetPath(self.tokens[low])
        if name in _SHAPES and name.startswith(("Vec", "Matrix")):
            # Small whole components, one signed byte each.
            raw = struct.pack("<Q", payload)
            if name.startswith("Vec"):
                n = _SHAPES[name][1]
                return tuple(float(b) for b in struct.unpack("<%db" % n, raw[:n]))
            n = int(name[6])
            diagonal = struct.unpack("<%db" % n, raw[:n])
            out = [0.0] * (n * n)
            for k in range(n):
                out[k * n + k] = float(diagonal[k])
            return tuple(out)
        if name == "Dictionary":
            return {}
        if name == "ValueBlock":
            return None
        return None

    def _count(self, at: int) -> Tuple[int, int]:
        (count,) = struct.unpack_from("<Q", self.data, at)
        return count, at + 8

    def _array(self, name: str, at: int, compressed: bool):
        if at == 0:
            return []
        count, at = self._count(at)
        data = self.data
        if name in ("Token", "String", "AssetPath"):
            indexes = struct.unpack_from("<%dI" % count, data, at)
            if name == "Token":
                return [self.tokens[i] for i in indexes]
            if name == "String":
                return [self.string(i) for i in indexes]
            return [AssetPath(self.tokens[i]) for i in indexes]
        if name in ("Int", "UInt", "Int64", "UInt64") and compressed:
            wide = name in ("Int64", "UInt64")
            found, _ = self._compressed_ints(at, count, wide)
            return found
        if name in ("Half", "Float", "Double") and compressed:
            code = data[at:at + 1]
            at += 1
            if code == b"i":
                found, _ = self._compressed_ints(at, count)
                return [float(v) for v in found]
            if code == b"t":
                (size,) = struct.unpack_from("<I", data, at)
                at += 4
                fmt = {"Half": "e", "Float": "f", "Double": "d"}[name]
                table = struct.unpack_from("<%d%s" % (size, fmt), data, at)
                at += size * struct.calcsize(fmt)
                found, _ = self._compressed_ints(at, count)
                return [table[i] for i in found]
            raise CrateError("An array compressed in a way this reader does not know.")
        shape = _SHAPES.get(name)
        if shape is None:
            return []
        fmt, n = shape
        flat = struct.unpack_from("<%d%s" % (count * n, fmt), data, at)
        if n == 1:
            return list(flat)
        if name.startswith("Quat"):
            return [_real_first(flat[k:k + 4]) for k in range(0, len(flat), 4)]
        return [flat[k:k + n] for k in range(0, len(flat), n)]

    def _at(self, name: str, at: int):
        data = self.data
        shape = _SHAPES.get(name)
        if shape is not None:
            fmt, n = shape
            flat = struct.unpack_from("<%d%s" % (n, fmt), data, at)
            if name.startswith("Quat"):
                return _real_first(flat)
            return flat[0] if n == 1 else flat
        if name == "Token":
            return self.tokens[struct.unpack_from("<I", data, at)[0]]
        if name == "String":
            return self.string(struct.unpack_from("<I", data, at)[0])
        if name == "AssetPath":
            return AssetPath(self.tokens[struct.unpack_from("<I", data, at)[0]])
        if name in ("TokenVector", "PathVector", "StringVector"):
            count, at = self._count(at)
            indexes = struct.unpack_from("<%dI" % count, data, at)
            if name == "TokenVector":
                return [self.tokens[i] for i in indexes]
            if name == "StringVector":
                return [self.string(i) for i in indexes]
            return [self.paths[i] for i in indexes]
        if name == "DoubleVector":
            count, at = self._count(at)
            return list(struct.unpack_from("<%dd" % count, data, at))
        if name.endswith("ListOp"):
            return self._list_op(name, at)
        if name == "VariantSelectionMap":
            count, at = self._count(at)
            out = {}
            for _ in range(count):
                key, value = struct.unpack_from("<II", data, at)
                at += 8
                out[self.string(key)] = self.string(value)
            return out
        if name == "Payload":
            reference, _ = self._reference(at, payload=True)
            return reference
        if name == "TimeSamples":
            return self._time_samples(at)
        return None

    def _reference(self, at: int, payload: bool) -> Tuple[Reference, int]:
        asset, prim = struct.unpack_from("<II", self.data, at)
        at += 8
        found = Reference(self.string(asset), self.paths[prim] if prim < len(self.paths) else "")
        if payload:
            if self.version >= (0, 8, 0):
                at += 16  # the layer offset
            return found, at
        at += 16  # the layer offset
        at = self._skip_dictionary(at)  # custom data
        return found, at

    def _skip_dictionary(self, at: int) -> int:
        count, at = self._count(at)
        for _ in range(count):
            at += 4  # the key, a string index
            (jump,) = struct.unpack_from("<q", self.data, at)
            # The value is written elsewhere; what follows the key is a jump
            # to it, and the next key comes after the jump.
            at += 8
            _ = jump
        return at

    def _list_op(self, name: str, at: int) -> ListOp:
        data = self.data
        header = data[at]
        at += 1
        op = ListOp()
        op.explicit = bool(header & 1)
        lists = []
        # In the order the file writes them: explicit, added, prepended,
        # appended, deleted, ordered.
        for bit, target in ((2, "items"), (4, "items"), (32, "prepended"),
                            (64, "appended"), (8, "deleted"), (16, None)):
            if header & bit:
                lists.append(target)
        for target in lists:
            count, at = self._count(at)
            items = []
            for _ in range(count):
                if name in ("ReferenceListOp", "PayloadListOp"):
                    item, at = self._reference(at, payload=name == "PayloadListOp")
                elif name in ("PathListOp",):
                    item = self.paths[struct.unpack_from("<I", data, at)[0]]
                    at += 4
                elif name in ("TokenListOp",):
                    item = self.tokens[struct.unpack_from("<I", data, at)[0]]
                    at += 4
                elif name == "StringListOp":
                    item = self.string(struct.unpack_from("<I", data, at)[0])
                    at += 4
                elif name in ("IntListOp", "UIntListOp"):
                    item = struct.unpack_from("<i" if name == "IntListOp" else "<I", data, at)[0]
                    at += 4
                elif name in ("Int64ListOp", "UInt64ListOp"):
                    item = struct.unpack_from("<q" if name == "Int64ListOp" else "<Q", data, at)[0]
                    at += 8
                else:
                    return op
                items.append(item)
            if target is not None:
                getattr(op, target).extend(items)
        return op

    def _time_samples(self, at: int) -> TimeSamples:
        data = self.data
        (jump,) = struct.unpack_from("<q", data, at)
        at += jump
        (times_rep,) = struct.unpack_from("<Q", data, at)
        at += 8
        times = self.value(times_rep)
        (jump,) = struct.unpack_from("<q", data, at)
        at += jump
        count, at = self._count(at)
        reps = struct.unpack_from("<%dQ" % count, data, at)
        out = TimeSamples()
        for t, rep in zip(times or [], reps):
            out[float(t)] = rep  # values are read when one is wanted
        out.crate = self  # type: ignore[attr-defined]
        return out

    # -- the answer --

    def layer(self) -> Dict[str, dict]:
        """Every spec: ``{path: {"kind": ..., "fields": {name: value}}}``.

        A field's value is read when it is asked for — a layer of a thousand
        meshes has thousands of arrays nobody draws — through ``fields``,
        which is a :class:`Fields` holding the reps.
        """
        out: Dict[str, dict] = {}
        sets = self.fieldsets
        for path_index, set_index, kind in self.specs:
            reps = {}
            at = set_index
            while at < len(sets) and sets[at] != -1 and sets[at] != 0xFFFFFFFF:
                token, rep = self.fields[sets[at]]
                reps[self.tokens[token]] = rep
                at += 1
            out[self.paths[path_index]] = {
                "kind": SPEC_TYPES[kind] if kind < len(SPEC_TYPES) else "Unknown",
                "fields": Fields(self, reps),
            }
        return out


class Fields(dict):
    """A spec's fields, each read the first time it is asked for."""

    def __init__(self, crate: Crate, reps: Dict[str, int]):
        super().__init__()
        self._crate = crate
        self._reps = reps

    def __contains__(self, key) -> bool:
        return key in self._reps

    def __getitem__(self, key):
        if not dict.__contains__(self, key):
            dict.__setitem__(self, key, self._crate.value(self._reps[key]))
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        return self[key] if key in self._reps else default

    def keys(self):
        return self._reps.keys()

    def __iter__(self):
        return iter(self._reps)

    def __len__(self):
        return len(self._reps)

    def items(self):
        return [(k, self[k]) for k in self._reps]


def sample(samples: TimeSamples, time: Optional[float] = None):
    """One value of a set of time samples: at ``time``, or the earliest."""
    if not samples:
        return None
    keys = sorted(samples)
    chosen = keys[0]
    if time is not None:
        for k in keys:
            if k <= time:
                chosen = k
    value = samples[chosen]
    crate = getattr(samples, "crate", None)
    return crate.value(value) if crate is not None and isinstance(value, int) else value
